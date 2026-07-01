"""TriagePR task for quick PR categorization and prioritization."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field
from shepherd_runtime.task.authoring import Input, Output, task

from ..models import PRDetails


@task(
    guidance="""You are triaging a pull request to determine its priority and categorize it.

Consider:
- Size and complexity of changes (files changed, additions/deletions)
- Type of change (feature, bugfix, refactor, docs, tests)
- Risk level based on files touched
- Whether it needs urgent attention
"""
)
class TriagePR(BaseModel):
    """Quickly assess and categorize a pull request.

    A lighter-weight alternative to full review - just categorizes
    and prioritizes the PR without detailed code analysis.

    Example:
        # First fetch PR details
        fetch = FetchPR(pr_number=123, repo="owner/repo")

        # Then triage
        triage = await TriagePR.arun(details=fetch.details)
        print(f"Category: {triage.category}")
        print(f"Priority: {triage.priority}")
        print(f"Risk: {triage.risk_level}")
    """

    # Inputs
    details: Input(PRDetails) = Field(description="PR details from FetchPR task")

    # Outputs
    category: Output(Literal["feature", "bugfix", "refactor", "docs", "tests", "chore", "other"]) = Field(
        default="other",
        description="Type of change",
    )
    priority: Output(Literal["critical", "high", "medium", "low"]) = Field(
        default="medium",
        description="Review priority",
    )
    risk_level: Output(Literal["high", "medium", "low"]) = Field(
        default="medium",
        description="Risk level of the changes",
    )
    estimated_review_time: Output(str) = Field(
        default="",
        description="Estimated time to review (e.g., '15 minutes', '1 hour')",
    )
    notes: Output(str) = Field(
        default="",
        description="Brief notes about the PR",
    )


__all__ = ["TriagePR"]
