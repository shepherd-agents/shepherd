"""Factory helpers for common vcs-core test setups."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from vcs_core._substrate_runtime import build_builtin_substrate_context
from vcs_core.store import Store
from vcs_core.substrates import DeclarativeFilesystemSubstrate, FilesystemSubstrate, MarkerSubstrate
from vcs_core.vcscore import VcsCore


def make_store(workspace: Path) -> Store:
    """Create a Store rooted at the workspace's test repository."""
    return Store(str(workspace / ".vcscore"))


def make_marker_filesystem_substrates(
    store: Store,
    *,
    declarative: bool = True,
    backend: Any | None = None,
    workspace: Path | None = None,
    config: dict[str, Any] | None = None,
) -> tuple[MarkerSubstrate, DeclarativeFilesystemSubstrate | FilesystemSubstrate]:
    """Create the default marker + filesystem substrate pair used in tests."""
    context = build_builtin_substrate_context(store, workspace=workspace, config=config)
    marker = MarkerSubstrate(context)
    if declarative:
        filesystem: DeclarativeFilesystemSubstrate | FilesystemSubstrate = DeclarativeFilesystemSubstrate(context)
    elif backend is None:
        filesystem = FilesystemSubstrate(context)
    else:
        filesystem = FilesystemSubstrate(context, backend=backend)
    return marker, filesystem


def make_vcscore(
    workspace: Path,
    *,
    substrates: list[object] | None = None,
    store: Store | None = None,
    activate: bool = False,
) -> VcsCore:
    """Create a VcsCore for the workspace, optionally activating it."""
    vcscore = VcsCore(str(workspace), substrates=substrates, store=store)
    if activate:
        vcscore.activate()
    return vcscore


def make_marker_filesystem_vcscore(
    workspace: Path,
    *,
    declarative: bool = True,
    backend: Any | None = None,
    activate: bool = False,
    store: Store | None = None,
) -> VcsCore:
    """Create a VcsCore with the default marker + filesystem substrates."""
    effective_store = store or make_store(workspace)
    marker, filesystem = make_marker_filesystem_substrates(
        effective_store,
        declarative=declarative,
        backend=backend,
    )
    return make_vcscore(
        workspace,
        substrates=[marker, filesystem],
        store=effective_store,
        activate=activate,
    )
