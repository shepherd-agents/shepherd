"""CLI porcelain for vcs-core.

Every CLI command maps to one or more VcsCore/Store method calls.
The CLI does not add semantics.
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING, Any

import click

from vcs_core import (
    _cli_command_effects,
    _cli_delegation,
    _cli_ipc,
    _cli_operations,
    _cli_scope_lifecycle,
    _cli_session_group,
)

if TYPE_CHECKING:
    from vcs_core.store import Store
from vcs_core._cli_config import config_group
from vcs_core._cli_errors import exit_app_error
from vcs_core._cli_session_group import session_group, switch_cmd
from vcs_core._cli_sub import sub
from vcs_core._cli_substrate import binding, substrate
from vcs_core._cli_workspace_boundary import (
    INIT_ENVIRONMENT_BOUNDARY_LINE,
    environment_boundary_line,
    managed_workspace_line,
)
from vcs_core._signals import terminate_as_interrupt


def _reject_if_session_running(command_name: str, *, guidance: str) -> None:
    """Fail closed for commands intentionally unsupported during a live session."""
    if _cli_ipc.live_session_info() is None:
        return
    click.echo(f"Error: `{command_name}` is not supported while a persistent session is active.")
    click.echo(f"  {guidance}")
    sys.exit(1)


def _exit_app_error(exc: Exception) -> None:
    exit_app_error(exc)


@click.group()
@click.version_option(package_name="vcs-core")
def main() -> None:
    """vcs-core: provenance-native version control for executable worlds."""


main.add_command(substrate)
main.add_command(binding)
main.add_command(config_group)
main.add_command(session_group)
main.add_command(sub)
main.add_command(switch_cmd)


# ---------------------------------------------------------------------------
# Top-level commands
# ---------------------------------------------------------------------------


@main.command()
@click.option(
    "--adopt",
    "adopt_source",
    type=click.Choice(["git-head", "worktree"]),
    default=None,
    help="Adopt an existing baseline into vcs-core's filesystem store.",
)
@click.option("--all", "adopt_all", is_flag=True, help="Adopt all supported files from the selected baseline.")
@click.argument("path", default=".", type=click.Path(exists=True))
def init(path: str, adopt_source: str | None, adopt_all: bool) -> None:
    """Initialize a vcs-core repository."""
    import os
    from pathlib import Path

    from vcs_core._identity import initialize_ground_world_id
    from vcs_core._workspace_adoption import adopt_workspace_baseline
    from vcs_core.config import load_config
    from vcs_core.discovery import discover_manifests
    from vcs_core.store import Store

    if adopt_all and adopt_source is None:
        click.echo("Error: `--all` requires `--adopt <git-head|worktree>`.")
        sys.exit(2)
    if adopt_source is not None and not adopt_all:
        click.echo("Error: baseline adoption currently requires `--all`.")
        sys.exit(2)
    if adopt_source is not None:
        _reject_if_session_running(
            "vcs-core init --adopt",
            guidance="Stop the session with `vcs-core session stop` before adopting workspace state.",
        )

    workspace = os.path.abspath(path)
    repo_path = os.path.join(workspace, ".vcscore")  # noqa: PTH118

    os.makedirs(repo_path, exist_ok=True)  # noqa: PTH103

    # Initialize bare repo
    store = Store(repo_path)
    created_store = store.is_empty
    if store.is_empty:
        store.create_root_commit()
    else:
        initialize_ground_world_id(repo_path)

    adoption_result = None
    if adopt_source is not None:
        if not created_store and _store_has_adopted_workspace_baseline(store):
            click.echo(
                "Error: this workspace already has an adopted filesystem baseline. "
                "Baseline replacement requires an explicit rebaseline command, which is not implemented yet."
            )
            sys.exit(1)
        try:
            adoption_result = adopt_workspace_baseline(
                store,
                Path(workspace),
                source=adopt_source,  # type: ignore[arg-type]
                acknowledge_materialized=True,
            )
        except (RuntimeError, ValueError) as exc:
            click.echo(f"Error: {exc}")
            sys.exit(1)

    # Write initial config if it doesn't exist
    config_path = Path(repo_path) / "config.toml"
    if not config_path.exists():
        config_path.write_text("# vcs-core local configuration\n# See vcscore.toml for project-level config\n")

    # Auto-detect substrates
    if created_store:
        click.echo(f"Initialized .vcscore/ repository in {workspace}")
    else:
        click.echo(f"Already initialized .vcscore/ repository in {workspace}")
    click.echo(managed_workspace_line(workspace))
    click.echo(INIT_ENVIRONMENT_BOUNDARY_LINE)
    click.echo()

    detected: list[str] = []
    manifests = discover_manifests(strict=False)
    for name, manifest in manifests.items():
        if manifest.tier == "always":
            click.echo(f"  + {name:20s} (always active)")
        elif manifest.tier == "auto-detect" and manifest.auto_detect and manifest.auto_detect(Path(workspace)):
            detected.append(name)
            click.echo(f"  + {name:20s} (auto-detected)")

    # Check project config
    project_toml = Path(workspace) / "vcscore.toml"
    if project_toml.exists():
        config = load_config(workspace)
        if config.bindings:
            click.echo()
            click.echo("Project config (vcscore.toml):")
            for name, binding_config in config.bindings.items():
                click.echo(f"  + {name:20s} ({binding_config.type})")

    if adoption_result is not None:
        click.echo()
        click.echo(
            f"Adopted {adoption_result.effect_count} filesystem change(s) "
            f"from {adoption_result.source} as the materialized baseline."
        )

    click.echo()
    click.echo("Run `vcs-core activate` to validate substrate configuration.")


@main.command()
@click.option(
    "--recover",
    type=click.Choice(["repair", "verify", "force"]),
    default=None,
    help="Recover dirty-push/materialization state before validating.",
)
@click.option(
    "--recover-lifecycle",
    type=click.Choice(["resume"]),
    default=None,
    help="Resume an interrupted merge/discard lifecycle before validating.",
)
@click.argument("path", default=".", type=click.Path(exists=True))
def activate(path: str, recover: str | None, recover_lifecycle: str | None) -> None:
    """Validate repository and substrate configuration."""
    from vcs_core._app import AppOpenMode, VcsCoreApp

    try:
        with VcsCoreApp.open_existing(
            path,
            mode=AppOpenMode.RECOVERY,
            recover=recover,
            recover_lifecycle=recover_lifecycle,
        ) as app:
            # TODO(R1b): activate should start a persistent session (daemon + overlay)
            substrates = app.mg.lifecycle_substrates
    except Exception as exc:  # noqa: BLE001
        _exit_app_error(exc)
    else:
        click.echo("Repository validated.")
        for sub in substrates:
            click.echo(f"  {getattr(sub, 'name', '?'):20s} active")


@main.command("recover-materialization")
@click.option(
    "--mode",
    type=click.Choice(["repair", "verify", "force"]),
    default="repair",
    show_default=True,
    help="Materialization recovery mode.",
)
@click.argument("path", default=".", type=click.Path(exists=True))
def recover_materialization(path: str, mode: str) -> None:
    """Recover dirty-push and materialization-run state."""
    _reject_if_session_running(
        "recover-materialization",
        guidance="Run recovery from the active session, or stop the session before using direct CLI recovery.",
    )
    from vcs_core.vcscore import VcsCore

    try:
        mg = VcsCore.from_config(os.path.abspath(path))
        report = mg.recover_materialization(mode=mode)
    except Exception as exc:  # noqa: BLE001
        _exit_app_error(exc)
    else:
        if not report.dirty_present and not report.run_present:
            click.echo("No materialization recovery state found.")
            return
        click.echo(f"Materialization recovery completed ({report.mode}).")
        if report.advanced_materialized:
            click.echo("  Advanced materialized ref.")
        if report.reset_ground:
            click.echo("  Reset ground ref to materialized.")
        if report.cleared_dirty:
            click.echo("  Cleared dirty push flag.")
        if report.cleared_run:
            click.echo("  Cleared materialization run ledger.")


@main.command()
def status() -> None:
    """Show pending changes and materialization state."""
    from vcs_core._app import AppOpenMode, VcsCoreApp

    if _cli_ipc.live_session_info() is not None:
        _cli_session_group.render_live_session_status()
        return

    try:
        with VcsCoreApp.open_existing(".", mode=AppOpenMode.CONTROL) as app:
            summary = app.repo_status()
    except Exception as exc:  # noqa: BLE001
        _exit_app_error(exc)
    else:
        click.echo(managed_workspace_line(Path(summary.workspace)))
        click.echo(environment_boundary_line())
        click.echo(f"Local changes: {summary.local_changes}")
        click.echo(f"Commits ahead: {summary.commits_ahead}")
        physical_blockers = tuple(blocker for blocker in summary.blockers if blocker.kind == "physical_workspace")
        if physical_blockers:
            click.echo("Physical workspace blockers:")
            for blocker in physical_blockers:
                click.echo(f"  {blocker.detail}")
                if blocker.hint:
                    click.echo(f"    {blocker.hint}")
        preflight_blockers = tuple(
            blocker for blocker in summary.blockers if blocker.kind == "materialization_preflight"
        )
        if preflight_blockers:
            click.echo("Materialization blockers:")
            for blocker in preflight_blockers:
                click.echo(f"  {blocker.detail}")
                if blocker.hint:
                    click.echo(f"    {blocker.hint}")
        recovery_blockers = tuple(
            blocker
            for blocker in summary.blockers
            if blocker.kind
            in {
                "dirty_push",
                "materialization_recovery",
                "operation_journal",
                "orphaned_scope",
                "orphaned_operation",
                "scope_registry_mismatch",
                "sibling_group",
                "workspace_authority",
            }
        )
        if recovery_blockers:
            click.echo("Recovery:")
            dirty_push = tuple(blocker for blocker in recovery_blockers if blocker.kind == "dirty_push")
            materialization_recovery = tuple(
                blocker for blocker in recovery_blockers if blocker.kind == "materialization_recovery"
            )
            operation_journals = tuple(blocker for blocker in recovery_blockers if blocker.kind == "operation_journal")
            orphaned_scopes = tuple(blocker for blocker in recovery_blockers if blocker.kind == "orphaned_scope")
            orphaned_operations = tuple(
                blocker for blocker in recovery_blockers if blocker.kind == "orphaned_operation"
            )
            mismatches = tuple(blocker for blocker in recovery_blockers if blocker.kind == "scope_registry_mismatch")
            sibling_groups = tuple(blocker for blocker in recovery_blockers if blocker.kind == "sibling_group")
            workspace_authority = tuple(
                blocker for blocker in recovery_blockers if blocker.kind == "workspace_authority"
            )
            if dirty_push:
                click.echo("  Dirty push:")
                for blocker in dirty_push:
                    click.echo(f"    {blocker.subject}")
            if materialization_recovery:
                click.echo("  Materialization recovery:")
                for blocker in materialization_recovery:
                    click.echo(f"    {blocker.subject}")
            if operation_journals:
                click.echo("  Operation journals:")
                for blocker in operation_journals:
                    click.echo(f"    {blocker.subject}")
            if orphaned_scopes:
                click.echo("  Orphaned scopes:")
                for blocker in orphaned_scopes:
                    click.echo(f"    {blocker.subject}")
            if orphaned_operations:
                click.echo("  Orphaned operations:")
                for operation in summary.orphaned_operations:
                    click.echo(f"    {_cli_operations.summary_identity(operation)}")
            if mismatches:
                click.echo("  Scope registry mismatches:")
                for blocker in mismatches:
                    click.echo(f"    {blocker.detail}")
            if sibling_groups:
                click.echo("  Sibling groups:")
                for blocker in sibling_groups:
                    click.echo(f"    {blocker.subject}")
            if workspace_authority:
                click.echo("  Pending workspace authority:")
                for blocker in workspace_authority:
                    click.echo(f"    {blocker.subject}")
            click.echo("  Run `vcs-core recovery` for details.")
        if summary.retained_scopes:
            click.echo("Retained scopes:")
            for entry in summary.retained_scopes:
                click.echo(f"  {entry.name}")


@main.command("archive-orphaned-scopes")
def archive_orphaned_scopes_cmd() -> None:
    """Archive orphaned scope refs from prior sessions."""
    from vcs_core._app import AppOpenMode, VcsCoreApp

    _reject_if_session_running(
        "vcs-core archive-orphaned-scopes",
        guidance="Stop the session with `vcs-core session stop` before running orphan cleanup.",
    )
    try:
        with VcsCoreApp.open_existing(".", mode=AppOpenMode.RECOVERY) as app:
            archived = app.archive_orphaned_scopes()
    except Exception as exc:  # noqa: BLE001
        _exit_app_error(exc)
    else:
        if not archived:
            click.echo("No orphaned scopes.")
            return
        click.echo(f"Archived {len(archived)} orphaned scope(s):")
        for name in archived:
            click.echo(f"  {name}")


@main.command("archive-orphaned-operations")
def archive_orphaned_operations_cmd() -> None:
    """Archive orphaned operation refs from prior sessions."""
    from vcs_core._app import AppOpenMode, VcsCoreApp

    _reject_if_session_running(
        "vcs-core archive-orphaned-operations",
        guidance="Stop the session with `vcs-core session stop` before running orphan cleanup.",
    )
    try:
        with VcsCoreApp.open_existing(".", mode=AppOpenMode.RECOVERY) as app:
            archived = app.archive_orphaned_operations()
    except Exception as exc:  # noqa: BLE001
        _exit_app_error(exc)
    else:
        if not archived:
            click.echo("No orphaned operations.")
            return
        click.echo(f"Archived {len(archived)} orphaned operation(s):")
        for label in archived:
            click.echo(f"  {label}")


@main.command()
@click.option("--substrate", "substrate_filter", default=None, help="Filter by substrate")
@click.option("--effect-type", default=None, help="Filter by effect type")
@click.option("--scope", "scope_filter", default=None, help="Filter by scope")
@click.option("-n", "--max-count", default=20, help="Maximum entries")
@click.option("--graph", is_flag=True, help="Show branch-structured graph view")
def log(
    substrate_filter: str | None,
    effect_type: str | None,
    scope_filter: str | None,
    max_count: int,
    graph: bool,
) -> None:
    """Show raw commit-carrier history, including retained structural records."""
    store = _open_store_readonly(".")

    if substrate_filter or effect_type or scope_filter:
        ref_filter = None
        if scope_filter is not None:
            # log is pure inspection: resolve the scope by name regardless of lifecycle
            # status (live / retained / terminal) so any scope's history is viewable.
            scope_entry = store.scope_registry_entry(scope_filter)
            if scope_entry is not None:
                ref_filter = scope_entry.ref
        entries = store.filter_effects(
            effect_type=effect_type,
            substrate=substrate_filter,
            ref=ref_filter,
            scope=scope_filter,
            max_count=max_count,
        )
    else:
        entries = store.log(max_count=max_count)

    if graph:
        from vcs_core._graph import render_graph

        for line in render_graph(entries):
            click.echo(line)
    else:
        for entry in entries:
            etype = entry.metadata.get("type", "?")
            escope = entry.metadata.get("scope", "?")
            click.echo(f"{entry.oid[:8]}  {etype:20s}  scope:{escope}")


@main.command("operations")
@click.option("--scope", "scope_name", default=None, help="Scope to query (default: ground, or current session scope)")
@click.option("--open", "show_open", is_flag=True, help="Show staged open operations")
@click.option("--archived", "show_archived", is_flag=True, help="Show archived operations")
@click.option("--all", "show_all", is_flag=True, help="Show visible, open, and archived sections")
@click.option("-n", "--max-count", default=20, help="Maximum operations per section")
def operations_cmd(
    scope_name: str | None, show_open: bool, show_archived: bool, show_all: bool, max_count: int
) -> None:
    """Show operation-shaped execution history across visible, staged, and archived operations."""
    _cli_operations.run_operations(
        scope_name=scope_name,
        show_open=show_open,
        show_archived=show_archived,
        show_all=show_all,
        max_count=max_count,
    )


@main.group("operation")
def operation_group() -> None:
    """Inspect operation summaries with carried commit history."""


@operation_group.command("show")
@click.argument("selector")
@click.option("--scope", "scope_name", default=None, help="Scope to constrain operation-id lookup when needed")
def operation_show_cmd(selector: str, scope_name: str | None) -> None:
    """Show one operation summary and its carried commit history."""
    _cli_operations.run_operation_show(selector=selector, scope_name=scope_name)


@main.command("recovery")
@click.option("-n", "--max-count", default=20, help="Maximum archived recovery operations to show")
def recovery_cmd(max_count: int) -> None:
    """Show non-canonical recovery/debug state for scopes and operations."""
    _cli_operations.run_recovery(max_count=max_count)


@main.command("recover-workspace-authority")
def recover_workspace_authority_cmd() -> None:
    """Resume pending required v2 workspace authority publication."""
    from vcs_core._app import AppOpenMode, VcsCoreApp

    _reject_if_session_running(
        "vcs-core recover-workspace-authority",
        guidance="Stop the session with `vcs-core session stop` before recovering workspace authority.",
    )
    try:
        with VcsCoreApp.open_existing(".", mode=AppOpenMode.RECOVERY) as app:
            recovered = app.mg.recover_workspace_authority()
    except Exception as exc:  # noqa: BLE001
        _exit_app_error(exc)
    else:
        if not recovered:
            click.echo("No pending workspace authority.")
            return
        click.echo(f"Recovered {len(recovered)} workspace authority operation(s):")
        for operation_id in recovered:
            click.echo(f"  {operation_id}")


@main.command("inspect")
@click.option(
    "--domain",
    "domains",
    multiple=True,
    type=click.Choice(
        ["all", "scope", "authority_ref", "world", "operation_journal", "workspace_authority", "recovery"]
    ),
    help="Inventory domain to inspect. May be repeated.",
)
@click.option("--scope", "scope_name", default="ground", help="Scope selector to inspect (default: ground).")
@click.option("--json", "as_json", is_flag=True, help="Render experimental JSON output.")
@click.option("--selector", default=None, help="Filter inventory items with the experimental selector syntax.")
def inspect_cmd(domains: tuple[str, ...], scope_name: str, as_json: bool, selector: str | None) -> None:
    """Inspect experimental vcs-core control-plane inventory."""
    import json

    from vcs_core._query_inspect import inspect_repository

    store = _open_store_readonly(".")
    try:
        payload = inspect_repository(store._repo_path, domains=domains, selector=selector, scope=scope_name)
    except Exception as exc:  # noqa: BLE001
        _exit_app_error(exc)
    if as_json:
        click.echo(json.dumps(payload, sort_keys=True, separators=(",", ":")))
        return
    items = payload["items"]
    if not isinstance(items, list) or not items:
        click.echo("No inventory items.")
        return
    for item in items:
        if isinstance(item, dict):
            click.echo(f"{item.get('domain')} {item.get('id')} {item.get('health', {}).get('status')}")


@main.command("readiness")
@click.option("--command", "command_name", default="shepherd.status", help="Readiness command class.")
@click.option("--scope", "scope_name", default="ground", help="Scope selector to evaluate.")
@click.option("--json", "as_json", is_flag=True, help="Render best-effort Shepherd readiness JSON.")
def readiness_cmd(command_name: str, scope_name: str, as_json: bool) -> None:
    """Evaluate best-effort first-cut Shepherd/vcs-core readiness."""
    import json

    from vcs_core._query_readiness import (
        ReadinessRequest,
        evaluate_readiness,
        known_readiness_commands,
        normalize_mutation_class,
    )

    # Validate the command class up front so an unknown value renders a clean
    # error rather than the raw ValueError traceback from deep in evaluation
    # (issue 04). Under --json, emit a structured error object so machine
    # consumers get a parseable answer on the error path too.
    try:
        normalize_mutation_class(command_name)
    except ValueError:
        valid = list(known_readiness_commands())
        if as_json:
            click.echo(
                json.dumps(
                    {"error": f"unknown readiness command: {command_name!r}", "valid_commands": valid},
                    sort_keys=True,
                    separators=(",", ":"),
                )
            )
            sys.exit(1)
        raise click.BadParameter(
            f"unknown readiness command {command_name!r}. Valid commands: {', '.join(valid)}",
            param_hint="'--command'",
        ) from None

    store = _open_store_readonly(".")
    try:
        request = ReadinessRequest.create(command=command_name, scope=scope_name, requested_freshness="best_effort")
        payload = evaluate_readiness(store._repo_path, request).to_json()
    except Exception as exc:  # noqa: BLE001
        _exit_app_error(exc)
    if as_json:
        click.echo(json.dumps(payload, sort_keys=True, separators=(",", ":")))
        return
    readiness = payload["readiness"]
    if isinstance(readiness, dict):
        click.echo(f"{readiness.get('command')} {readiness.get('state')} allowed={readiness.get('allowed')}")
    blockers = payload["blockers"]
    if isinstance(blockers, list) and blockers:
        click.echo("Blockers:")
        for blocker in blockers:
            if isinstance(blocker, dict):
                click.echo(f"  {blocker.get('kind')} {blocker.get('item_id')}")


@main.command()
def diff() -> None:
    """Show file changes since last push."""
    _reject_if_session_running(
        "vcs-core diff",
        guidance="Use `vcs-core session status` while a persistent session is active.",
    )
    store = _open_store_readonly(".")
    d = store.diff()
    for f in d.files:
        click.echo(f"  {f.status:10s} {f.path}")
    if not d.files:
        click.echo("No changes.")


@main.command()
@click.argument("ref")
@click.option(
    "--dest", default=None, type=click.Path(), help="Destination directory (default: .vcscore/checkouts/<ref>)"
)
def checkout(ref: str, dest: str | None) -> None:
    """Extract workspace state at a historical ref to a directory.

    REF can be "ground", "materialized", a scope name, an archive name,
    or a commit OID (full or short prefix).
    The extracted files are a snapshot for inspection.
    """
    store = _open_store_readonly(".")
    resolved = _resolve_checkout_ref(store, ref)
    if resolved is None:
        click.echo(f"Error: cannot resolve ref '{ref}'.")
        # Check for ambiguous archive matches
        archive_prefix = f"refs/vcscore/archive/{ref}"
        archive_matches = [r for r in store.list_archive_refs() if r.startswith(archive_prefix)]
        if len(archive_matches) > 1:
            names = [r.rsplit("/", 1)[-1] for r in archive_matches]
            click.echo(f"  Ambiguous archive ref. Matches: {', '.join(names)}")
        else:
            scope_refs = store.list_scope_refs()
            if scope_refs:
                names = [r.rsplit("/", 1)[-1] for r in scope_refs]
                click.echo(f"  Available scopes: {', '.join(names)}")
            archive_refs = store.list_archive_refs()
            if archive_refs:
                names = [r.rsplit("/", 1)[-1] for r in archive_refs]
                click.echo(f"  Available archives: {', '.join(names)}")
        click.echo("  Built-in refs: ground, materialized")
        sys.exit(1)

    if dest is None:
        ref_short = ref.replace("/", "-")[:24]
        dest = os.path.join(".vcscore-checkouts", ref_short)  # noqa: PTH118

    from vcs_core._errors import RefResolutionError

    try:
        count = store.checkout_workspace_tree(resolved, dest)
    except RefResolutionError as exc:
        click.echo(f"Error: {exc}")
        sys.exit(1)
    except ValueError as exc:
        click.echo(f"Error: {exc}")
        sys.exit(1)
    click.echo(f"Extracted {count} files to {dest}")


def _resolve_checkout_ref(store: Store, user_ref: str) -> str | None:
    """Map a user-facing ref name, OID, or short OID prefix to a commitish.

    Returns a string that Store.resolve_to_commit() can resolve, or None.
    """
    from vcs_core.store import GROUND_REF, MATERIALIZED_REF

    if user_ref == "ground":
        return GROUND_REF
    if user_ref == "materialized":
        return MATERIALIZED_REF
    # Try as a scope name
    scope_ref = f"refs/vcscore/scopes/{user_ref}"
    if store.ref_exists(scope_ref):
        return scope_ref
    # Try as an archive ref (prefix match — archive names include instance IDs)
    archive_prefix = f"refs/vcscore/archive/{user_ref}"
    archive_matches = [r for r in store.list_archive_refs() if r.startswith(archive_prefix)]
    if len(archive_matches) == 1:
        return archive_matches[0]
    # Try as a raw commit OID or short prefix
    if store.resolve_to_commit(user_ref) is not None:
        return user_ref
    return None


@main.command()
@click.option("--dry-run", is_flag=True, help="Preview without executing")
@click.option("--up-to", default=None, help="Stop before phase (auto, compensable, none)")
def push(dry_run: bool, up_to: str | None) -> None:
    """Materialize pending operations to substrate remotes.

    In Store-only mode (R1a), push advances the materialized ref.
    In overlay mode (R1b+), push syncs workspace changes to filesystem
    materialization targets.
    """
    _reject_if_session_running(
        "vcs-core push",
        guidance="Stop the session with `vcs-core session stop` before materializing to the physical workspace.",
    )

    def _render_push_result(result: dict[str, Any]) -> None:
        total = result.get("total_operations", 0)
        if dry_run:
            phases = result.get("phase_count", 0)
            click.echo(f"Would materialize {total} operations in {phases} phases.")
            if result.get("has_irreversible"):
                click.echo("  WARNING: includes irreversible operations")
        else:
            click.echo(f"Materialized {total} operations.")

    def _fallback() -> None:
        from vcs_core._app import AppOpenMode, VcsCoreApp

        try:
            with VcsCoreApp.open_existing(".", mode=AppOpenMode.CONTROL) as app:
                result = app.push(dry_run=dry_run, up_to=up_to)
        except Exception as exc:  # noqa: BLE001
            _exit_app_error(exc)
        else:
            plan = result.plan
            if dry_run:
                click.echo(f"Would materialize {plan.total_operations} operations in {len(plan.phases)} phases:")
                for phase in plan.phases:
                    label = phase.reversibility.upper()
                    click.echo(f"  Phase ({label}): {phase.operation_count} ops")
                if plan.has_irreversible:
                    click.echo("  WARNING: includes irreversible operations")
            else:
                click.echo(f"Materialized {plan.total_operations} operations.")

    _cli_delegation.with_session_result(
        "push",
        {"dry_run": dry_run, "up_to": up_to},
        on_result=_render_push_result,
        on_fallback=_fallback,
    )


@main.command()
def coverage() -> None:
    """Report platform containment status."""
    import platform

    workspace = os.path.abspath(".")
    repo_path = os.path.join(workspace, ".vcscore")  # noqa: PTH118
    if not os.path.exists(repo_path):  # noqa: PTH110
        click.echo("Error: not a vcs-core repository. Run `vcs-core init` first.")
        sys.exit(1)

    click.echo(f"Platform: {sys.platform} ({platform.machine()})")
    click.echo()
    click.echo(
        f"  {'Substrate':<14s} {'Contain':<10s} {'Gated':<7s} {'Prov':<10s} {'C-Tier':<10s} {'P-Tier':<10s} Summary"
    )
    click.echo(f"  {'─' * 110}")

    for report in _resolve_authority_reports(workspace):
        gated = "yes" if report.containment.access_gated else "no"
        click.echo(
            f"  {report.substrate:<14s} {report.containment.regime:<10s} {gated:<7s} "
            f"{report.provenance.regime:<10s} {report.containment.tier:<10s} "
            f"{report.provenance.tier:<10s} {report.reason}"
        )


# ---------------------------------------------------------------------------
# Scoped execution
# ---------------------------------------------------------------------------


@main.command("run")
@click.argument("script", type=click.Path())
@click.argument("args", nargs=-1, type=click.UNPROCESSED)
@click.option("--scope", "scope_name", default=None, help="Scope name (default: run-<script>)")
@click.option("--parent", default=None, help="Parent scope (default: ground)")
@click.option(
    "--on-error",
    type=click.Choice(["keep", "discard"]),
    default="keep",
    help="On script failure: keep scope for inspection or discard it",
)
def run_cmd(
    script: str,
    args: tuple[str, ...],
    scope_name: str | None,
    parent: str | None,
    on_error: str,
) -> None:
    """Run a Python script with filesystem interception active.

    File operations (open, os.remove, shutil.*, etc.) within the workspace
    are automatically captured as effects. The script runs in-process via
    runpy, so Python-level patches are active for the duration.

    On success the scope is merged into its parent. On failure the scope
    is kept for inspection (default) or discarded.
    """
    _reject_if_session_running(
        "vcs-core run",
        guidance="Use `vcs-core session exec`, `vcs-core session shell`, or stop the session first.",
    )

    import runpy
    from pathlib import Path

    from vcs_core._app import AppError, AppOpenMode, VcsCoreApp

    script_path = Path(script)
    if not script_path.exists():
        click.echo(f"Error: script does not exist: {script}")
        sys.exit(1)

    stem = Path(script).stem
    scope_name = scope_name or f"run-{stem}"

    try:
        # Starting a run reclaims a dead prior run's orphaned operation refs first, so an
        # interrupted run does not wedge the next one ("just run it again"). The session-lock
        # gate in activate() makes this safe: a genuinely live session is refused, not reclaimed.
        app_context = VcsCoreApp.open_existing(
            ".", mode=AppOpenMode.CONTROL, auto_recover_orphaned_operations=True
        )
        # SIGTERM (`kill`/`docker stop`/systemd/k8s) otherwise terminates without unwinding
        # and orphans the open operation; route it through Ctrl-C's clean-discard path.
        with terminate_as_interrupt(), app_context as app:
            mg = app.mg
            parent_name = parent or "ground"
            parent_scope = app.resolve_scope(parent_name)
            result = app.branch(name=scope_name, parent=parent_name)
            scope = mg.lookup_scope(result.name)
            if scope is None:
                raise RuntimeError(f"Created scope {scope_name!r} is not active.")

            # Fork installs an explicit execution context for this run. Substrate
            # Python patches were installed at activate time, so intercepted file
            # operations record to the run scope automatically.
            saved_argv = sys.argv[:]
            script_abs = os.path.abspath(script)
            script_error: BaseException | None = None
            script_exit_code: int | None = None
            try:
                sys.argv = [script_abs, *args]
                with mg.runtime_activity(
                    scope=scope,
                    operation_label=f"run-{stem}",
                    operation_kind="python.run",
                    failure_policy="complete_error",
                    operation_metadata={"script": script_abs, "argv": list(args)},
                ):
                    try:
                        runpy.run_path(script_abs, run_name="__main__")
                    except SystemExit as exc:
                        if exc.code not in (None, 0):
                            raise
            except SystemExit as exc:
                if exc.code not in (None, 0):
                    script_exit_code = int(exc.code) if isinstance(exc.code, int) else 1
                    script_error = exc
            except Exception as exc:  # noqa: BLE001
                script_error = exc
                script_exit_code = 1
            finally:
                sys.argv = saved_argv

            if script_error is not None:
                if on_error == "discard":
                    mg.discard(scope)
                    if isinstance(script_error, SystemExit):
                        click.echo(f"Script exited with code {script_exit_code}. Discarded scope '{scope_name}'.")
                    else:
                        click.echo(f"Script failed: {script_error}. Discarded scope '{scope_name}'.")
                elif isinstance(script_error, SystemExit):
                    click.echo(f"Script exited with code {script_exit_code}. Scope '{scope_name}' kept for inspection.")
                else:
                    click.echo(f"Script failed: {script_error}. Scope '{scope_name}' kept for inspection.")
                sys.exit(script_exit_code)

            mg.merge(scope, parent_scope)
            effects = mg.store.filter_effects(scope=scope_name, ref=parent_scope.ref)
            file_effects = [
                e for e in effects if e.metadata.get("type") in ("FileCreate", "FilePatch", "FileDelete", "FileRead")
            ]
            click.echo(f"Merged '{scope_name}': {len(file_effects)} file effect(s) captured")
    except AppError as exc:
        _exit_app_error(exc)
    except ValueError as exc:
        click.echo(f"Error: {exc}")
        sys.exit(1)


# ---------------------------------------------------------------------------
# Scope lifecycle commands
# ---------------------------------------------------------------------------


@main.command()
@click.argument("name")
@click.option("--parent", default=None, help="Parent scope (default: ground)")
@click.option(
    "--isolated/--no-isolated", default=False, help="Create isolated scope (overlay; requires session for isolation)"
)
def branch(name: str, parent: str | None, isolated: bool) -> None:
    """Create a new scope (speculative branch)."""
    _cli_scope_lifecycle.run_branch(name=name, parent=parent, isolated=isolated)


@main.command("merge")
@click.argument("name")
def merge_cmd(name: str) -> None:
    """Merge a scope into its parent."""
    _cli_scope_lifecycle.run_merge(name=name)


@main.command("discard")
@click.argument("name")
def discard_cmd(name: str) -> None:
    """Discard a scope."""
    _cli_scope_lifecycle.run_discard(name=name)


@main.command("exec")
@click.argument("binding_name")
@click.argument("command")
@click.option("-p", "--param", multiple=True, help="key=value parameter")
@click.option(
    "--scope",
    "scope_name",
    default=None,
    help="Scope to operate on (default: ground; required when live scopes exist)",
)
@click.option("--non-reversible-run", is_flag=True, help="Run execution-bound commands directly on the target scope.")
@click.option("--json", "as_json", is_flag=True, help="Render machine-readable JSON output.")
def exec_cmd(
    binding_name: str,
    command: str,
    param: tuple[str, ...],
    scope_name: str | None,
    non_reversible_run: bool,
    as_json: bool,
) -> None:
    """Execute a binding command and record effects."""
    _cli_command_effects.run_exec(
        binding_name=binding_name,
        command=command,
        raw_params=param,
        scope_name=scope_name,
        non_reversible_run=non_reversible_run,
        as_json=as_json,
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _store_has_adopted_workspace_baseline(store: Store) -> bool:
    """Return true when init adoption would replace an existing baseline."""
    if store.filter_effects(effect_type="WorkspaceBaselineAdopt", substrate="filesystem", max_count=1):
        return True
    return bool(store.list_workspace_files(store.GROUND_REF))


def _open_store_readonly(workspace: str) -> Store:
    """Open the Store for read-only queries. No session lock, no activation."""
    import os

    from vcs_core._errors import InvalidRepositoryStateError
    from vcs_core.store import Store

    repo_path = os.path.join(os.path.abspath(workspace), ".vcscore")  # noqa: PTH118
    if not os.path.exists(repo_path):  # noqa: PTH110
        click.echo("Error: not a vcs-core repository. Run `vcs-core init` first.")
        sys.exit(1)
    try:
        store = Store.open_existing(repo_path)
    except (FileNotFoundError, InvalidRepositoryStateError) as exc:
        click.echo(f"Error: {exc}")
        sys.exit(1)
    return store


def _resolve_authority_reports(workspace: str) -> list[Any]:
    from vcs_core.config import load_config
    from vcs_core.discovery import resolve_bindings
    from vcs_core.store import Store

    with tempfile.TemporaryDirectory(prefix="vcs-core-coverage-") as tmpdir:
        repo_path = os.path.join(tmpdir, ".vcscore")  # noqa: PTH118
        os.makedirs(repo_path, exist_ok=True)  # noqa: PTH103
        store = Store(repo_path)
        config = load_config(workspace)
        bindings = resolve_bindings(config, Path(workspace), store)
        return [binding.instance.authority() for binding in bindings]
