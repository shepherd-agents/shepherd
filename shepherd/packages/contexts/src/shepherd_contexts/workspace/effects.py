"""Workspace-specific effects."""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal

from pydantic import Field
from shepherd_core.effects import (
    PREVIEW_LENGTH_TOOL_OUTPUT,
    DiffPatch,
    Effect,
)

if TYPE_CHECKING:
    from collections.abc import Mapping


class WorkspacePatchCaptured(Effect):
    """Git diff was captured from workspace."""

    effect_type: Literal["workspace_patch_captured"] = "workspace_patch_captured"
    files_changed: tuple[str, ...] = ()
    patch_hash: str = ""
    patch_size_bytes: int = 0
    patch: DiffPatch = Field(default_factory=lambda: DiffPatch(patch="", files_changed=()))
    caused_by: str | None = None


class BashCommand(Effect):
    """Bash command was executed."""

    effect_type: Literal["bash_command"] = "bash_command"
    command: str = ""
    exit_code: int = 0
    output: str = ""
    caused_by: str | None = None

    @property
    def output_preview(self) -> str:
        """Truncated preview for display."""
        if len(self.output) > PREVIEW_LENGTH_TOOL_OUTPUT:
            return self.output[:PREVIEW_LENGTH_TOOL_OUTPUT] + "..."
        return self.output


def get_effect_types() -> Mapping[str, type[Effect]]:
    """Return the explicit effect contributor surface for runtime decode."""
    return {
        "bash_command": BashCommand,
        "workspace_patch_captured": WorkspacePatchCaptured,
    }


__all__ = [
    "BashCommand",
    "WorkspacePatchCaptured",
    "get_effect_types",
]
