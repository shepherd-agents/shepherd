"""RefineDocuments task — apply targeted edits based on critique feedback."""

from __future__ import annotations

from pydantic import BaseModel, Field
from shepherd_contexts import WorkspaceRef  # noqa: TC002
from shepherd_runtime.task.authoring import Context, Input, Output, task

from ..models import CritiqueOutput


@task(
    guidance="""You are refining design documents based on critique feedback.

Read the critique's issues, suggestions, and reasoning context carefully.
Read each document using the Read tool.

## Priority

Address issues in this order:
1. Issues with status "new" or "unchanged" — these are the most important.
2. Issues with status "partially_resolved" — make further progress.
3. Skip issues with status "resolved".
4. Suggestions, if appropriate.

## Editing Rules

Make targeted edits using the Edit tool — do NOT rewrite entire files.
Preserve content you don't need to change.

When addressing a critique:
- RESTRUCTURE or REMOVE problematic content. Do not address issues by
  adding disclaimers, caveats, or "non-normative" labels — that is not
  a fix, it is avoidance.
- If a section is too implementation-prescriptive, either remove the
  prescriptive detail or restructure it as a concrete example clearly
  separated from the design contract.
- If a claim is unsupported, either add the supporting evidence or
  remove the claim.
- If documents are inconsistent, resolve the inconsistency — do not
  add notes acknowledging it.
"""
)
class RefineDocuments(BaseModel):
    """Apply targeted edits to documents based on critique feedback."""

    # Inputs
    document_paths: Input(dict[str, str]) = Field(description="name -> path mapping")
    critique: Input(CritiqueOutput) = Field(description="Full critique output")
    principles: Input(list[str]) = Field(description="Guiding principles")

    # Context
    workspace: Context[WorkspaceRef]

    # Outputs
    edited_paths: Output(list[str]) = Field(default=[], description="Paths modified")
    change_summary: Output(str) = Field(default="", description="Brief description of changes")


__all__ = ["RefineDocuments"]
