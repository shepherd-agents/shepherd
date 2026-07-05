"""Host-side workspace bootstrap and capability probes for CLI/quickstart use.

Dialect is the designated vcs-core integration home (`test_d2_boundary`), so the
public ``shepherd`` package and the quickstart templates route their vcs-core
needs through here instead of importing ``vcs_core`` directly. Everything here
uses only the **public** ``vcs_core.runtime_api`` surface — no ``vcs_core._*``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from pathlib import Path

WorkspaceAdoptMode = Literal["none", "git-head", "worktree"]


def native_jail_available() -> bool:
    """Whether a native syscall-jail backend is usable on this host."""
    from vcs_core.runtime_api import native_jail_available as _impl

    return _impl()


@dataclass(frozen=True)
class WorkspaceInitResult:
    """Outcome of :func:`initialize_workspace`: store status and adoption count."""

    status: str  # "created" | "already initialized"
    adopted_count: int
    already_initialized: bool


class WorkspaceInitError(RuntimeError):
    """Raised when workspace bootstrap cannot proceed (fail-closed)."""


def initialize_workspace(
    workspace: Path,
    *,
    adopt: WorkspaceAdoptMode = "none",
    explicit_adopt: bool = False,
) -> WorkspaceInitResult:
    """Create/validate the ``.vcscore`` store, optionally adopting a baseline.

    Encapsulates the store lifecycle so callers never touch vcs-core internals.
    Raises ``WorkspaceInitError`` on invalid adoption state.
    """
    from vcs_core.runtime_api import (
        Store,
        adopt_workspace_baseline,
        initialize_ground_world_id,
    )

    repo_path = workspace / ".vcscore"
    repo_path.mkdir(exist_ok=True)
    store = Store(str(repo_path))
    created_store = store.is_empty
    if created_store:
        store.create_root_commit()
    else:
        initialize_ground_world_id(str(repo_path))

    config_path = repo_path / "config.toml"
    if not config_path.exists():
        config_path.write_text("# vcs-core local configuration\n# See vcscore.toml for project-level config\n")

    adopted_count = 0
    if adopt != "none":
        if not created_store:
            if explicit_adopt:
                raise WorkspaceInitError(
                    "baseline adoption is only supported while creating a fresh .vcscore repository"
                )
            return WorkspaceInitResult("already initialized", 0, already_initialized=True)
        try:
            result = adopt_workspace_baseline(store, workspace, source=adopt, acknowledge_materialized=True)
        except (RuntimeError, ValueError) as exc:
            raise WorkspaceInitError(str(exc)) from exc
        adopted_count = result.effect_count

    return WorkspaceInitResult(
        "created" if created_store else "already initialized",
        adopted_count,
        already_initialized=not created_store,
    )
