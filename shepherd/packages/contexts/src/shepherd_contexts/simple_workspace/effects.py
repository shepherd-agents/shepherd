"""Effect types for SimpleWorkspace context.

This module defines effects specific to SimpleWorkspace:
- SimpleWorkspaceChangesetCaptured: Changeset from execution (contains full delta data)
- SimpleWorkspaceInitialized: Workspace bound with base manifest
- SimpleWorkspaceMaterialized: Changes written to filesystem (audit only)
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, ClassVar, Literal

from pydantic import computed_field
from shepherd_core.effects import Effect

if TYPE_CHECKING:
    from collections.abc import Mapping


class SimpleWorkspaceChangesetCaptured(Effect):
    """Changeset captured from SimpleWorkspace execution.

    Contains FULL DELTA DATA - not just metadata.
    This enables state reconstruction via apply_effect().

    Analogous to WorkspacePatchCaptured but for non-git workspaces.
    """

    model_config: ClassVar[dict[str, bool]] = {"arbitrary_types_allowed": True}

    effect_type: Literal["simple_workspace_changeset_captured"] = "simple_workspace_changeset_captured"

    changeset: Any = None  # FileChangeset | None - using Any to avoid Pydantic validation issues

    @computed_field  # type: ignore[prop-decorator]
    @property
    def files_changed(self) -> tuple[str, ...]:
        """Files affected by this changeset."""
        if self.changeset:
            return self.changeset.files_changed
        return ()

    @computed_field  # type: ignore[prop-decorator]
    @property
    def total_size_bytes(self) -> int:
        """Total encoded size of changeset."""
        if self.changeset:
            return self.changeset.total_size_bytes
        return 0

    @computed_field  # type: ignore[prop-decorator]
    @property
    def is_empty(self) -> bool:
        """Whether changeset has no changes."""
        return self.changeset is None or self.changeset.is_empty


class SimpleWorkspaceInitialized(Effect):
    """SimpleWorkspace initialized with base manifest.

    Emitted when workspace is first bound to scope.
    Contains the initial state for reconstruction.
    """

    model_config: ClassVar[dict[str, bool]] = {"arbitrary_types_allowed": True}

    effect_type: Literal["simple_workspace_initialized"] = "simple_workspace_initialized"

    base_manifest: Any = None  # FileManifest | None - using Any to avoid Pydantic validation issues
    path: str = ""


class SimpleWorkspaceMaterialized(Effect):
    """SimpleWorkspace changes materialized to filesystem.

    Emitted when user explicitly materializes pending changes.
    Audit trail only - state change happens via materialize() return value.
    """

    effect_type: Literal["simple_workspace_materialized"] = "simple_workspace_materialized"

    changesets_applied: int = 0
    files_affected: tuple[str, ...] = ()


def get_effect_types() -> Mapping[str, type[Effect]]:
    """Return the explicit effect contributor surface for runtime decode."""
    return {
        "simple_workspace_changeset_captured": SimpleWorkspaceChangesetCaptured,
        "simple_workspace_initialized": SimpleWorkspaceInitialized,
        "simple_workspace_materialized": SimpleWorkspaceMaterialized,
    }
