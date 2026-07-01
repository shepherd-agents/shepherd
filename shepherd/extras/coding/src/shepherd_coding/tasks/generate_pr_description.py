"""GeneratePRDescription / generate_pr_description task."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated

from pydantic import BaseModel, Field
from shepherd_runtime.nucleus import deliver
from shepherd_runtime.nucleus import task as nucleus_task
from shepherd_runtime.task.authoring import Input, Output
from shepherd_runtime.task.authoring import task as class_task
from shepherd_runtime.task.markers import InputMarker

_PR_DESC_GUIDANCE = """\
You are a senior engineer writing a pull request description.
Be concise, specific, and focus on the why, not just the what.
The title should be in conventional commit format (feat:, fix:, etc.).

IMPORTANT: For large PRs, the diff may be truncated. Use the commit log
and the complete changed_files list as your primary source of truth for
what the PR contains. The diff is supplementary context for understanding
how specific changes were made, not the authoritative list of what changed.
Group changes by logical area (feature, bugfix, refactor, infrastructure).
"""


@dataclass(frozen=True)
class PRDescriptionResult:
    """Generated pull request title and body."""

    pr_title: str = ""
    pr_body: str = ""


@nucleus_task(guidance=_PR_DESC_GUIDANCE)
async def generate_pr_description(
    diff_text: Annotated[str, InputMarker(description="Unified diff of all changes")] = "",
    commit_log: Annotated[str, InputMarker(description="Commit log, one-line format")] = "",
    changed_files: Annotated[
        list[str] | None,
        InputMarker(description="Complete list of changed file paths"),
    ] = None,
    verdict: Annotated[str, InputMarker(description="Quality gate verdict")] = "ready",
    tool_summary: Annotated[str, InputMarker(description="Summary of tool results")] = "",
    unresolved_summary: Annotated[str, InputMarker(description="Summary of unresolved issues")] = "",
) -> PRDescriptionResult:
    """Generate a PR title and body from the diff and commit history."""
    files = changed_files or []
    return await deliver(
        PRDescriptionResult,
        goal="Generate a concise PR title and Markdown body with Summary, Changes, and Test Plan sections.",
        evidence=[
            f"diff_text={diff_text}",
            f"commit_log={commit_log}",
            f"changed_files={files}",
            f"verdict={verdict}",
            f"tool_summary={tool_summary}",
            f"unresolved_summary={unresolved_summary}",
        ],
    )


@class_task(guidance=_PR_DESC_GUIDANCE)
class GeneratePRDescription(BaseModel):
    """Class-form compatibility wrapper for workflow-pipeline callers."""

    diff_text: Input(str) = Field(description="Unified diff of all changes")
    commit_log: Input(str) = Field(default="", description="Commit log (one-line format)")
    changed_files: Input(list[str]) = Field(default=[], description="List of changed file paths")
    verdict: Input(str) = Field(default="ready", description="Quality gate verdict")
    tool_summary: Input(str) = Field(default="", description="Summary of tool results")
    unresolved_summary: Input(str) = Field(default="", description="Summary of unresolved issues")

    pr_title: Output(str) = Field(default="", description="Concise PR title under 70 characters")
    pr_body: Output(str) = Field(
        default="", description="Markdown PR body with Summary, Changes, and Test Plan sections"
    )


__all__ = ["GeneratePRDescription", "PRDescriptionResult", "generate_pr_description"]
