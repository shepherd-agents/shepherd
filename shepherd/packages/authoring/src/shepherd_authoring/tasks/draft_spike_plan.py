"""DraftSpikePlan task — draft a spike plan to validate design assumptions."""

from __future__ import annotations

from pydantic import BaseModel, Field
from shepherd_contexts import WorkspaceRef  # noqa: TC002
from shepherd_runtime.task.authoring import Context, Input, Output, task


@task(
    guidance="""You are drafting a spike plan to validate assumptions in a design.

Read the design document and principles using the Read tool.

If source_paths are provided, READ EACH ONE using the Read tool before
writing the spike plan. These are the actual source files the design
discusses. Understanding the real code is critical — your spikes should
identify specific failure modes, edge cases, and behavioral traps visible
in the code, not generic methodology.

Identify the key assumptions, risks, and unknowns in the design.

Each spike must have:
- Title
- Hypothesis/question to validate
- Method (what work to do — be SPECIFIC about what to test, what code
  paths to exercise, and what failure modes to probe. Reference actual
  function names, variable names, and control flow from the source files
  when available.)
- Evidence produced (what artifacts prove the hypothesis)
- Decision point (what threshold determines success/failure)

Write the spike plan to the output_dir path as SPIKES.md using the Write tool.
"""
)
class DraftSpikePlan(BaseModel):
    """Draft a spike plan that derisks the design's key assumptions."""

    # Inputs
    design_document_path: Input(str) = Field(description="Path to design doc")
    principles: Input(list[str]) = Field(description="Guiding principles")
    output_dir: Input(str) = Field(default="", description="Directory to write SPIKES.md into")
    source_paths: Input(list[str]) = Field(
        default_factory=list,
        description="Paths to source files referenced by the design (read these for concrete spike targets)",
    )

    # Context
    workspace: Context[WorkspaceRef]

    # Outputs
    spike_plan_path: Output(str) = Field(default="", description="Path to SPIKES file")


__all__ = ["DraftSpikePlan"]
