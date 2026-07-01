"""RunDesignRefinement — execute a refinement plan."""

from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import BaseModel, Field
from shepherd_contexts import WorkspaceRef  # noqa: TC002
from shepherd_runtime.task.authoring import Context, Input, Output, task

from shepherd_authoring.checks import check_file_exists


@task
class RunDesignRefinement(BaseModel):
    """Execute a refinement plan produced by PlanDesignRefinement.

    All outputs (REFINEMENT-LOG.md, .versions/) are written into the
    output_dir specified in the plan YAML, keeping the source workspace clean.
    """

    # Inputs
    plan_path: Input(str) = Field(description="Path to REFINEMENT-PLAN.yaml")

    # Context
    workspace: Context[WorkspaceRef]

    # Outputs
    final_score: Output(float) = Field(default=0.0)
    converged: Output(bool) = Field(default=False)
    iterations_used: Output(int) = Field(default=0)
    log_path: Output(str) = Field(default="")
    output_dir: Output(str) = Field(default="")

    def execute(self) -> None:
        # Precondition: plan file exists
        plan_file = Path(self.plan_path)
        if not check_file_exists(plan_file):
            raise FileNotFoundError(f"Plan file not found: {self.plan_path}")

        plan = yaml.safe_load(plan_file.read_text())

        # Output dir is stored in the plan; fall back to plan file's parent
        out_dir = plan.get("output_dir", str(plan_file.parent))
        self.output_dir = out_dir

        # Read principles from the structured list in the plan YAML.
        # Falls back to parsing the principles file only if the list is missing
        # (backwards compat with hand-written plans).
        principles = plan.get("principles")
        if not principles:
            principles_path = plan.get("principles_path", "")
            if principles_path and Path(principles_path).exists():
                principles_content = Path(principles_path).read_text()
                principles = [
                    line.strip()
                    for line in principles_content.splitlines()
                    if line.strip() and not line.startswith("#")
                ]

        if not principles:
            raise ValueError(
                "No principles found in plan. Ensure PlanDesignRefinement "
                "ran successfully or that the plan YAML contains a 'principles' list."
            )

        # Precondition: all referenced documents exist
        document_paths = plan.get("document_paths", {})
        for name, doc_path in document_paths.items():
            if doc_path and not check_file_exists(Path(doc_path)):
                raise FileNotFoundError(f"Document '{name}' not found at: {doc_path}")

        from shepherd_authoring.workflows.design_refinement.critique_refine_loop import CritiqueRefineLoop

        loop = self.run_stage_sync(
            "critique_refine",
            CritiqueRefineLoop,
            document_paths=document_paths,
            principles=principles,
            max_iterations=plan.get("max_iterations", 5),
            target_score=plan.get("target_score", 8.0),
            workspace_path=out_dir,
        )

        self.final_score = loop.final_score
        self.converged = loop.converged
        self.iterations_used = loop.iterations_used
        self.log_path = str(Path(out_dir) / "REFINEMENT-LOG.md")


__all__ = ["RunDesignRefinement"]
