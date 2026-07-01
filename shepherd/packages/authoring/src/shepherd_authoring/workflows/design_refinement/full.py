"""DesignRefinement — full plan-then-refine workflow in one call.

Chains PlanDesignRefinement → RunDesignRefinement so users don't
need to manage the two-phase handoff manually.
"""

from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, Field
from shepherd_contexts import WorkspaceRef  # noqa: TC002
from shepherd_runtime.task.authoring import Context, Input, Output, task
from shepherd_runtime.task.pipeline import OnError

from .plan import PlanDesignRefinement
from .run import RunDesignRefinement


@task
class DesignRefinement(BaseModel):
    """Plan and execute iterative design document refinement.

    Extracts principles, drafts a spike plan, then runs a critique-refine
    loop until quality converges or the iteration budget is exhausted.
    The original document is never modified — all work happens in a
    .refinement/{doc-name}/ directory.
    """

    # Inputs
    design_document_path: Input(str) = Field(description="Path to design document")
    max_iterations: Input(int) = Field(default=5, description="Maximum refinement iterations")
    target_score: Input(float) = Field(default=8.0, description="Quality score to converge on (1-10)")

    # Context
    workspace: Context[WorkspaceRef]

    # Outputs
    final_score: Output(float) = Field(default=0.0)
    converged: Output(bool) = Field(default=False)
    iterations_used: Output(int) = Field(default=0)
    plan_path: Output(str) = Field(default="")
    log_path: Output(str) = Field(default="")
    output_dir: Output(str) = Field(default="")

    def execute(self) -> None:
        design_path = Path(self.design_document_path)
        if not design_path.exists():
            raise FileNotFoundError(f"Design document not found: {self.design_document_path}")

        # Phase 1: Plan
        plan = self.run_stage_sync(
            "plan",
            PlanDesignRefinement,
            on_error=OnError.fatal,
            design_document_path=self.design_document_path,
            max_iterations=self.max_iterations,
            target_score=self.target_score,
        )
        self.plan_path = plan.plan_path
        self.output_dir = plan.output_dir

        # Phase 2: Execute
        run = self.run_stage_sync(
            "run",
            RunDesignRefinement,
            on_error=OnError.fatal,
            plan_path=plan.plan_path,
        )
        self.final_score = run.final_score
        self.converged = run.converged
        self.iterations_used = run.iterations_used
        self.log_path = run.log_path


__all__ = ["DesignRefinement"]
