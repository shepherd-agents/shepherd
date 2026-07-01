"""ExtractPrinciples task — extract guiding principles from a design document."""

from __future__ import annotations

from pydantic import BaseModel, Field
from shepherd_contexts import WorkspaceRef  # noqa: TC002
from shepherd_runtime.task.authoring import Context, Input, Output, task


@task(
    guidance="""You are extracting guiding principles from a design document.

Read the design document at the provided path using the Read tool.
Identify the most important guiding principles — the core invariants,
constraints, and design values that shape the document's decisions.

Return EXACTLY 3 to 5 principles. Focus on the principles that matter
most for evaluating document quality. Each principle should be a single
concise sentence that a critic could evaluate against.

Write a PRINCIPLES.md file to the output_dir path using the Write tool.
Format as a numbered list.
"""
)
class ExtractPrinciples(BaseModel):
    """Extract guiding principles from a design document and write them to disk."""

    # Inputs
    design_document_path: Input(str) = Field(description="Path to design doc on disk")
    output_dir: Input(str) = Field(default="", description="Directory to write PRINCIPLES.md into")

    # Context
    workspace: Context[WorkspaceRef]

    # Outputs
    principles_path: Output(str) = Field(default="", description="Path to written PRINCIPLES file")
    principles: Output(list[str]) = Field(default=[], description="Extracted principles (3-5 items)")


__all__ = ["ExtractPrinciples"]
