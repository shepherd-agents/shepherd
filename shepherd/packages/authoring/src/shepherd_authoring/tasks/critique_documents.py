"""CritiqueDocuments task — evaluate documents for quality."""

from __future__ import annotations

from pydantic import BaseModel, Field
from shepherd_contexts import WorkspaceRef  # noqa: TC002
from shepherd_runtime.task.authoring import Context, Input, Output, task

from ..models import CritiqueIssue  # noqa: TC001 (runtime: Pydantic resolves Output annotations)

_GUIDANCE = """You are a document critic evaluating design documents.

Read each document at the paths provided using the Read tool.
Evaluate the documents holistically, using your own judgment about what
constitutes a high-quality design document. The principles provided are
a lens for evaluation, not a checklist — use them to focus your attention
but rely on your own sense of correctness, clarity, and completeness.

Focus on:
- Are claims accurate and well-supported?
- Do documents agree with each other?
- Are important topics covered without significant gaps?
- Can each document be understood on its own?
- Is the "why" clearly articulated?
- Does the level of detail serve the document's purpose?

Detail and specificity are valuable in a design document. Flag
implementation CODE (actual function bodies, class definitions) but do
NOT penalize detailed descriptions of contracts, data flows, directory
layouts, or migration steps — those are design content, not implementation.

## Issue Tracking

If `plateau` is True and `prior_issues` are provided, you are in TRIAGE
MODE. For each prior issue, evaluate whether it has been addressed:
- Set status to "resolved" if adequately addressed.
- Set status to "partially_resolved" if some progress was made but the
  core concern remains.
- Set status to "unchanged" if no meaningful progress was made.

In triage mode, raise at most ONE new issue (status "new"), and only if
it is clearly more important than any remaining unresolved prior issue.
Score based on unresolved issues only — do not hold the score down for
inherent tensions that the refiner has addressed as well as reasonably
possible.

If `plateau` is False or no `prior_issues` are given, perform a fresh
evaluation. All issues should have status "new".

Return a single 1-10 score alongside structured issues and suggestions.

Do NOT modify any files — this is a read-only evaluation.

If prior_reasoning is provided, use it as context for your evaluation
but form your own independent judgment.
"""


@task(guidance=_GUIDANCE)
class CritiqueDocuments(BaseModel):
    """Evaluate documents for quality, returning structured critique."""

    # Inputs
    document_paths: Input(dict[str, str]) = Field(description="name -> path mapping")
    principles: Input(list[str]) = Field(description="Guiding principles to evaluate against")
    prior_reasoning: Input(str | None) = Field(default=None, description="Prior critique reasoning")
    prior_issues: Input(list[str]) = Field(
        default_factory=list,
        description="Issue descriptions from the previous iteration (empty on first iteration)",
    )
    plateau: Input(bool) = Field(
        default=False,
        description="If True, use triage mode — triage prior issues rather than raising new ones",
    )

    # Context
    workspace: Context[WorkspaceRef]

    # Outputs
    score: Output(float) = Field(default=0.0, description="Holistic quality score, 1-10")
    issues: Output(list[CritiqueIssue]) = Field(default=[], description="Tracked issues with status")
    suggestions: Output(list[str]) = Field(default=[], description="Non-blocking improvements")
    reasoning_context: Output(str) = Field(default="", description="Chain-of-thought for refiner")


__all__ = ["CritiqueDocuments"]
