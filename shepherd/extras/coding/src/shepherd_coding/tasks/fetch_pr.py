"""FetchPR task for retrieving pull request details from GitHub."""

from __future__ import annotations

from typing import Annotated

from pydantic import BaseModel, Field
from shepherd_runtime.task.authoring import Input, Output, task

from ..models import PRDetails  # noqa: TC001
from ..utils import get_pr_details


@task
class FetchPR(BaseModel):
    """Fetch detailed information about a GitHub pull request.

    This is a programmatic task - it calls the GitHub API directly
    without involving an LLM. The execute() method signals this.

    Example:
        # Fetch with explicit repo
        pr = FetchPR(pr_number=123, repo="owner/repo")
        print(f"PR #{pr.details.number}: {pr.details.title}")

        # Infer repo from git remote
        pr = FetchPR(pr_number=456)
        print(f"PR: {pr.details.title}")
    """

    # Inputs
    pr_number: Input(int) = Field(description="Pull request number to fetch")
    repo: Input(str | None) = Field(
        default=None,
        description="Repository in owner/repo format. Inferred from git remote if not provided.",
    )

    # Outputs
    details: Annotated[
        Output[PRDetails],
        Field(
            description="Complete PR details including files, commits, and reviews",
        ),
    ]

    def execute(self) -> None:
        """Fetch PR details from GitHub API."""
        self.details = get_pr_details(self.pr_number, self.repo)


__all__ = ["FetchPR"]
