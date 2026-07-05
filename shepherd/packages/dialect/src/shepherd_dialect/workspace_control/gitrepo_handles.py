"""GitRepo value-noun hydration helpers for workspace-control surfaces."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from shepherd_dialect.workspace_control.errors import WorkspaceControlError

if TYPE_CHECKING:
    from shepherd_runtime.nucleus import GitRepo, GitRepoBasis

WORKSPACE_GIT_REPO_BINDING = "workspace"
_READ_AUTHORITY = frozenset({"read"})
_READ_WRITE_AUTHORITY = frozenset({"read", "write"})


def selected_workspace_git_repo(mg: Any) -> GitRepo:
    """Hydrate the currently selected workspace binding as a GitRepo value noun."""
    from shepherd_runtime.nucleus import GitRepo

    return GitRepo(
        binding=WORKSPACE_GIT_REPO_BINDING,
        basis=selected_workspace_git_repo_basis(mg),
        authority=_READ_WRITE_AUTHORITY,
    )


def named_subroot_git_repo(mg: Any, name: str) -> GitRepo:
    """Hydrate a named sub-root binding as a GitRepo value noun (Lane C, LC-1).

    A named binding is a *view* of the selected whole-workspace custody, scoped to a disjoint
    sub-root the workspace records (``name -> realpath(root)``). Because custody is whole-workspace
    (confirmed by the Lane C custody spike), the basis is the selected workspace basis; the binding
    *name* plus the workspace-recorded root distinguish the view. Authority is the binding's full
    declared vocabulary (read+write) — per-parameter ``May[...]`` grants clamp it at spawn
    (§6.2). The root deliberately does not live on the value noun ("GitRepo is always a
    value"); the workspace holds it and threads it to the jail/authority lowering in LC-2/LC-3.
    """
    from shepherd_runtime.nucleus import GitRepo

    return GitRepo(
        binding=name,
        basis=selected_workspace_git_repo_basis(mg),
        authority=_READ_WRITE_AUTHORITY,
    )


def require_selected_workspace_git_repo(mg: Any, repo: Any) -> GitRepo:
    """Validate that ``repo`` is the current selected workspace GitRepo input."""
    from shepherd_runtime.nucleus import GitRepo

    if not isinstance(repo, GitRepo):
        raise WorkspaceControlError("workspace run requires a GitRepo value for repo")
    if repo.binding != WORKSPACE_GIT_REPO_BINDING:
        raise WorkspaceControlError("workspace run currently requires the workspace GitRepo binding")
    if not _READ_WRITE_AUTHORITY.issubset(repo.authority):
        raise WorkspaceControlError("workspace run currently requires advisory read/write GitRepo authority")
    selected_basis = selected_workspace_git_repo_basis(mg)
    if not same_git_binding_state(repo.basis, selected_basis):
        raise WorkspaceControlError("workspace run requires a GitRepo at the current selected workspace binding state")
    return repo


def selected_workspace_git_repo_basis(mg: Any) -> GitRepoBasis:
    """Resolve the selected workspace binding into the first GitRepo basis shape."""
    from shepherd_runtime.nucleus import GitRepoBasis

    world_reader = getattr(mg, "world_oid", None)
    if not callable(world_reader):
        raise WorkspaceControlError("VcsCore.world_oid is required for selected workspace GitRepo hydration")
    world_oid = world_reader()
    if world_oid is None:
        raise WorkspaceControlError("selected workspace GitRepo hydration requires a current workspace world")

    selected_reader = getattr(mg, "read_selected_binding_revision_with_head", None)
    if not callable(selected_reader):
        raise WorkspaceControlError(
            "VcsCore.read_selected_binding_revision_with_head is required for selected workspace GitRepo hydration"
        )
    selected = selected_reader(WORKSPACE_GIT_REPO_BINDING)
    if selected is None:
        raise WorkspaceControlError("selected workspace GitRepo hydration requires a selected workspace binding")
    if selected.binding != WORKSPACE_GIT_REPO_BINDING:
        raise WorkspaceControlError("selected workspace GitRepo binding identity disagrees with workspace binding")
    return GitRepoBasis(
        world_oid=world_oid,
        store_id=selected.store_id,
        resource_id=selected.resource_id,
        head=selected.head,
    )


def retained_output_git_repo_basis(output: Any) -> GitRepoBasis:
    """Resolve a retained workspace RunOutput into the first GitRepo basis shape."""
    from shepherd_runtime.nucleus import GitRepoBasis

    return GitRepoBasis(
        world_oid=output.output_world_oid,
        store_id=output.store_id,
        resource_id=output.resource_id,
        head=output.identity.candidate_head,
    )


def same_git_binding_state(left: GitRepoBasis, right: GitRepoBasis) -> bool:
    """Return true when two GitRepo bases name the same binding revision state.

    Full ``GitRepoBasis`` equality includes ``world_oid`` provenance. Selection
    can create a new parent world that carries the same Git binding head as a
    retained output, so callers that only need Git state equivalence compare the
    store/resource/head triple.
    """
    return left.store_id == right.store_id and left.resource_id == right.resource_id and left.head == right.head


def readonly_git_repo_for_retained_output(output: Any) -> GitRepo:
    """Hydrate a retained workspace RunOutput as a read-only GitRepo value noun."""
    from shepherd_runtime.nucleus import GitRepo

    return GitRepo(
        binding=output.binding,
        basis=retained_output_git_repo_basis(output),
        authority=_READ_AUTHORITY,
    )
