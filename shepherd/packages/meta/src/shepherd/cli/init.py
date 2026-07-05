"""``sp init`` — initialize a Shepherd workspace."""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Literal

import click
from click.core import ParameterSource

WorkspaceAdoptMode = Literal["none", "git-head", "worktree"]
WorkspaceBackendOption = Literal["auto", "clonefile", "fuse", "kernel", "copy"]


@click.command()
@click.argument("path", default=".", required=False, type=click.Path(file_okay=False, path_type=Path))
@click.option(
    "--backend",
    type=click.Choice(["auto", "clonefile", "fuse", "kernel", "copy"]),
    default="auto",
    show_default=True,
    help="Filesystem carrier backend to validate with.",
)
@click.option(
    "--adopt",
    type=click.Choice(["none", "git-head", "worktree"]),
    default="worktree",
    show_default=True,
    help="Record an existing project baseline into Shepherd custody.",
)
@click.option(
    "--init-git/--no-init-git",
    default=True,
    show_default=True,
    help="Initialize a Git repository when PATH is not already inside one.",
)
@click.pass_context
def init(
    ctx: click.Context,
    path: Path,
    backend: WorkspaceBackendOption,
    adopt: WorkspaceAdoptMode,
    init_git: bool,
) -> None:
    """Initialize PATH as a Shepherd workspace.

    This creates or reuses ``.vcscore`` in PATH, validates the workspace-control
    substrate, and leaves ordinary Git history untouched. Use
    ``sp package init NAME`` for package scaffolding.
    """
    workspace = path.expanduser().resolve()
    if not workspace.exists():
        raise click.ClickException(f"{workspace} does not exist")
    if not workspace.is_dir():
        raise click.ClickException(f"{workspace} is not a directory")

    git_state = _ensure_git_workspace(workspace, init_git=init_git)
    vcscore_state, adopted_count = _initialize_vcscore(
        workspace,
        adopt=adopt,
        explicit_adopt=ctx.get_parameter_source("adopt") is ParameterSource.COMMANDLINE,
    )
    _validate_workspace(workspace, backend=backend)

    click.echo(f"Initialized Shepherd workspace: {workspace}")
    click.echo(f"  git:      {git_state}")
    click.echo(f"  vcscore:  {vcscore_state}")
    click.echo(f"  backend:  {backend}")
    if adopt == "none":
        click.echo("  adoption: none")
    else:
        click.echo(f"  adoption: {adopt} ({adopted_count} filesystem change(s))")
    click.echo()
    click.echo("Next:")
    click.echo("  sp demo write quickstart > quickstart_demo.py")
    click.echo("  python quickstart_demo.py")
    click.echo("  sp run list")


def _ensure_git_workspace(workspace: Path, *, init_git: bool) -> str:
    git_root = _git_root(workspace)
    if git_root is not None:
        return f"existing ({git_root})"
    if not init_git:
        raise click.ClickException(
            f"{workspace} is not inside a Git repository. Run `git init` first or pass `--init-git`."
        )

    result = subprocess.run(
        ["git", "init"],
        cwd=workspace,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or "git init failed"
        raise click.ClickException(detail)
    return "initialized"


def _git_root(workspace: Path) -> Path | None:
    result = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"],
        cwd=workspace,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        return None
    raw = result.stdout.strip()
    if not raw:
        return None
    return Path(raw).resolve()


def _initialize_vcscore(
    workspace: Path,
    *,
    adopt: WorkspaceAdoptMode,
    explicit_adopt: bool,
) -> tuple[str, int]:
    # Route store bootstrap through the dialect integration home; the meta
    # package imports no vcs-core surface directly (test_d2_boundary).
    from shepherd_dialect import WorkspaceInitError, initialize_workspace

    try:
        result = initialize_workspace(workspace, adopt=adopt, explicit_adopt=explicit_adopt)
    except WorkspaceInitError as exc:
        raise click.ClickException(str(exc)) from exc
    return result.status, result.adopted_count


def _validate_workspace(workspace: Path, *, backend: WorkspaceBackendOption) -> None:
    from shepherd_dialect.workspace_control import ShepherdWorkspace

    selected_backend = None if backend == "auto" else backend
    try:
        opened = ShepherdWorkspace.discover(workspace, activate=True, backend=selected_backend)
    except Exception as exc:
        raise click.ClickException(f"workspace initialized but activation failed: {exc}") from exc
    finally:
        close = locals().get("opened")
        if close is not None:
            close.close()


__all__ = ["init"]
