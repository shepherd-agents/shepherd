"""Workspace baseline adoption for initializing filesystem state."""

from __future__ import annotations

import os
import uuid
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from vcs_core._identity import read_ground_world_id
from vcs_core._workspace_external import (
    ExternalWorkspace,
    ExternalWorkspaceFile,
    validate_relative_path,
)
from vcs_core.store import GROUND_REF, MATERIALIZED_REF, Store
from vcs_core.types import EffectRecord, WorkspaceChange

if TYPE_CHECKING:
    from pathlib import Path

AdoptionSource = Literal["git-head", "worktree"]

WorkspaceFileState = ExternalWorkspaceFile


@dataclass(frozen=True)
class AdoptionResult:
    """Summary of one baseline adoption recording pass."""

    source: AdoptionSource
    effect_count: int
    oids: tuple[str, ...] = ()


def read_git_head_source(workspace: Path) -> dict[str, WorkspaceFileState]:
    """Read regular files from the workspace Git HEAD tree."""
    return ExternalWorkspace(workspace).read_git_head_source()


def read_worktree_source(workspace: Path) -> dict[str, WorkspaceFileState]:
    """Read regular files from the physical workspace, excluding control dirs."""
    return ExternalWorkspace(workspace).read_worktree_source()


def read_adoption_source(workspace: Path, source: AdoptionSource) -> dict[str, WorkspaceFileState]:
    return ExternalWorkspace(workspace).read_adoption_source(source)


def plan_adoption_effects(
    store: Store,
    source_files: dict[str, WorkspaceFileState],
    *,
    source: AdoptionSource,
) -> tuple[EffectRecord, ...]:
    """Build filesystem effects that make ground exactly match source_files."""
    ground_paths = {path for path, _oid, _mode in store.list_workspace_files(GROUND_REF)}
    selected_paths = sorted(ground_paths | set(source_files))
    workspace_changes: list[WorkspaceChange] = []
    created_count = 0
    patched_count = 0
    deleted_count = 0

    for raw_path in selected_paths:
        path = validate_relative_path(raw_path)
        source_file = source_files.get(path)
        current_content = store.read_workspace_file(GROUND_REF, path)
        current_mode = store.workspace_file_mode(GROUND_REF, path)

        if source_file is None:
            if current_content is not None:
                workspace_changes.append((path, None))
                deleted_count += 1
            continue

        workspace_change: WorkspaceChange = (path, source_file.content, source_file.mode)
        if current_content is None:
            workspace_changes.append(workspace_change)
            created_count += 1
            continue

        if current_content != source_file.content or current_mode != source_file.mode:
            workspace_changes.append(workspace_change)
            patched_count += 1

    return (
        EffectRecord(
            effect_type="WorkspaceBaselineAdopt",
            metadata={
                "source": source,
                "path_count": len(workspace_changes),
                "created_count": created_count,
                "patched_count": patched_count,
                "deleted_count": deleted_count,
            },
            workspace_changes=tuple(workspace_changes),
        ),
    )


def _workspace_change_count(effects: tuple[EffectRecord, ...]) -> int:
    return sum(len(effect.workspace_changes) for effect in effects)


def _ensure_clean_adoption_baseline(store: Store) -> None:
    status = store.status()
    if status.local_changes == 0 and status.commits_ahead == 0:
        return
    raise RuntimeError(
        "Baseline adoption requires ground and materialized to match. "
        "Push, reset, or use a fresh vcs-core repository before adopting workspace state."
    )


def adopt_workspace_baseline(
    store: Store,
    workspace: Path,
    *,
    source: AdoptionSource,
    acknowledge_materialized: bool = True,
) -> AdoptionResult:
    """Record source files as ordinary filesystem effects on ground."""
    external_workspace = ExternalWorkspace(workspace)
    if acknowledge_materialized:
        _ensure_clean_adoption_baseline(store)
    if source == "git-head":
        blockers = external_workspace.git_status_blockers(reason="git-worktree-dirty")
        if blockers:
            sample = ", ".join(blocker.path for blocker in blockers[:5])
            remainder = len(blockers) - min(len(blockers), 5)
            suffix = f", and {remainder} more" if remainder > 0 else ""
            raise ValueError(
                "Cannot adopt Git HEAD while the selected workspace has dirty or untracked "
                f"non-ignored path(s): {sample}{suffix}. Clean the worktree or use "
                "`vcs-core init --adopt worktree --all` to adopt physical state."
            )

    source_files = external_workspace.read_adoption_source(source)
    effects = plan_adoption_effects(store, source_files, source=source)
    effect_count = _workspace_change_count(effects)
    if not effects:
        return AdoptionResult(source=source, effect_count=0)

    world_id = read_ground_world_id(store.repo_path)
    operation_id = f"filesystem-adopt-{source}-{uuid.uuid4().hex[:8]}"
    op = store.begin_operation(
        GROUND_REF,
        handle_id=operation_id,
        kind="filesystem.adopt",
        world_id=world_id,
        scope_instance_id="ground",
        operation_id=operation_id,
        operation_label=f"filesystem-adopt-{source}",
        metadata={
            "source": source,
            "workspace": os.fspath(workspace),
            "acknowledged_materialized": acknowledge_materialized,
        },
    )

    oids: list[str] = []
    try:
        for effect in effects:
            oids.append(
                store.append_operation_effect(
                    op,
                    effect.effect_type,
                    effect.metadata,
                    workspace_changes=list(effect.workspace_changes),
                    substrate="filesystem",
                )
            )
        oids.append(
            store.finalize_operation(
                op,
                metadata={
                    "source": source,
                    "effect_count": effect_count,
                    "acknowledged_materialized": acknowledge_materialized,
                },
            )
        )
    except Exception:
        store.abort_operation(op, metadata={"source": source})
        raise

    _select_workspace_adoption_state(
        store,
        workspace,
        operation_id=operation_id,
        source=source,
        advance_materialized=acknowledge_materialized,
    )

    if acknowledge_materialized:
        store.advance_materialized()

    return AdoptionResult(source=source, effect_count=effect_count, oids=tuple(oids))


def materialized_matches_ground(store: Store) -> bool:
    return str(store._repo.references[GROUND_REF].target) == str(store._repo.references[MATERIALIZED_REF].target)


def _select_workspace_adoption_state(
    store: Store,
    workspace: Path,
    *,
    operation_id: str,
    source: AdoptionSource,
    advance_materialized: bool,
) -> None:
    from vcs_core.types import ScopeInfo
    from vcs_core.vcscore import VcsCore

    vcscore = VcsCore(os.fspath(workspace), store=store)
    vcscore._select_workspace_state_from_store_required(
        scope=ScopeInfo(
            name="ground",
            ref=GROUND_REF,
            instance_id="ground-adoption",
            creation_oid="",
            world_id=read_ground_world_id(store.repo_path),
        ),
        operation_id=f"wv_adopt_{operation_id}",
        source_operation_id=operation_id,
        driver_command="adopt-baseline",
        message=f"workspace adoption: {source}",
        advance_materialized=advance_materialized,
    )
