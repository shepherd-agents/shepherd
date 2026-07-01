"""DesignReview — extract principles and critique a design document.

A lightweight alternative to the full design refinement pipeline.
Produces a quality score, issues, and suggestions without iterating.
"""

from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, Field
from shepherd_contexts import WorkspaceRef  # noqa: TC002
from shepherd_runtime.task.authoring import Context, Input, Output, task
from shepherd_runtime.task.pipeline import OnError

from shepherd_authoring.tasks.critique_documents import CritiqueDocuments
from shepherd_authoring.tasks.extract_principles import ExtractPrinciples


@task
class DesignReview(BaseModel):
    """Review a design document: extract principles, then critique against them.

    Returns a quality score (1-10), issues, and suggestions. Does not
    modify any files — this is a read-only assessment.
    """

    # Inputs
    design_document_path: Input(str) = Field(description="Path to design document")
    principles: Input(list[str] | None) = Field(
        default=None,
        description="Explicit principles to evaluate against. If None, extracted automatically.",
    )

    # Context
    workspace: Context[WorkspaceRef]

    # Outputs
    score: Output(float) = Field(default=0.0, description="Quality score, 1-10")
    issues: Output(list[str]) = Field(default=[], description="Blocking problems")
    suggestions: Output(list[str]) = Field(default=[], description="Non-blocking improvements")
    extracted_principles: Output(list[str]) = Field(default=[], description="Principles used")

    def execute(self) -> None:
        design_path = Path(self.design_document_path)
        if not design_path.exists():
            raise FileNotFoundError(f"Design document not found: {self.design_document_path}")

        # Use explicit principles or extract them
        principles = self.principles
        if not principles:
            extract = self.run_stage_sync(
                "extract_principles",
                ExtractPrinciples,
                on_error=OnError.fatal,
                design_document_path=self.design_document_path,
                output_dir=str(design_path.parent),
            )
            principles = extract.principles or []

        if not principles:
            raise ValueError("No principles could be extracted from the design document.")

        self.extracted_principles = principles

        # Critique
        critique = self.run_stage_sync(
            "critique",
            CritiqueDocuments,
            on_error=OnError.fatal,
            document_paths={"design": self.design_document_path},
            principles=principles,
        )

        self.score = critique.score if critique.score is not None else 0.0
        self.issues = critique.issues if critique.issues is not None else []
        self.suggestions = critique.suggestions if critique.suggestions is not None else []


__all__ = ["DesignReview"]
