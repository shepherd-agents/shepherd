"""Triage task — quick categorization and risk assessment of code changes.

Decoupled from ``TriagePR``'s ``PRDetails`` dependency.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field
from shepherd_runtime.task.authoring import Input, Output, task


@task(
    guidance="""You are triaging code changes to determine priority and risk.

Consider:
- Size and complexity of changes (files changed, additions/deletions)
- Type of change (feature, bugfix, refactor, docs, tests)
- Risk level based on files touched (core logic vs peripheral)
- Whether it needs urgent attention

If PR context is provided, also consider the PR title, labels, and author.
"""
)
class Triage(BaseModel):
    """Quickly assess and categorize code changes."""

    file_change_summary: Input(str) = Field(description="File list with change status and line counts")
    pr_context: Input(str | None) = Field(default=None, description="Formatted PR metadata. None for branch triage.")

    category: Output(Literal["feature", "bugfix", "refactor", "docs", "tests", "chore", "other"]) = Field(
        default="other"
    )
    priority: Output(Literal["critical", "high", "medium", "low"]) = Field(default="medium")
    risk_level: Output(Literal["high", "medium", "low"]) = Field(default="medium")
    notes: Output(str) = Field(default="")


__all__ = ["Triage"]
