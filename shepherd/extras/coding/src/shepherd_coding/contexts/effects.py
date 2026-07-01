"""GitHub-specific effects.

Effects representing actions taken on GitHub - reviewing PRs,
posting comments, merging, etc. These are recorded in the effect
stream for observability and audit trails.

All effects are frozen Pydantic models with effect_type discriminator.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal

from pydantic import Field
from shepherd_core import Effect

if TYPE_CHECKING:
    from collections.abc import Mapping


class PRReviewSubmitted(Effect):
    """A review was submitted to a pull request."""

    effect_type: Literal["pr_review_submitted"] = "pr_review_submitted"
    pr_number: int = 0
    repo: str = ""
    state: str = ""  # APPROVED, CHANGES_REQUESTED, COMMENTED
    body: str = ""


class PRCommented(Effect):
    """A comment was posted on a pull request.

    Can be a general comment or a line-specific review comment.
    """

    effect_type: Literal["pr_commented"] = "pr_commented"
    pr_number: int = 0
    repo: str = ""
    body: str = ""
    path: str | None = None  # File path for line comments
    line: int | None = None  # Line number for line comments
    side: str | None = None  # LEFT or RIGHT for diff comments


class PRMerged(Effect):
    """A pull request was merged."""

    effect_type: Literal["pr_merged"] = "pr_merged"
    pr_number: int = 0
    repo: str = ""
    merge_commit_sha: str = ""
    merge_method: str = ""  # merge, squash, rebase


class PRClosed(Effect):
    """A pull request was closed without merging."""

    effect_type: Literal["pr_closed"] = "pr_closed"
    pr_number: int = 0
    repo: str = ""


class PRLabeled(Effect):
    """Labels were added to a pull request."""

    effect_type: Literal["pr_labeled"] = "pr_labeled"
    pr_number: int = 0
    repo: str = ""
    labels: list[str] = Field(default_factory=list)


class PRUnlabeled(Effect):
    """Labels were removed from a pull request."""

    effect_type: Literal["pr_unlabeled"] = "pr_unlabeled"
    pr_number: int = 0
    repo: str = ""
    labels: list[str] = Field(default_factory=list)


def get_effect_types() -> Mapping[str, type[Effect]]:
    """Return the explicit effect contributor surface for runtime decode."""
    return {
        "pr_closed": PRClosed,
        "pr_commented": PRCommented,
        "pr_labeled": PRLabeled,
        "pr_merged": PRMerged,
        "pr_review_submitted": PRReviewSubmitted,
        "pr_unlabeled": PRUnlabeled,
    }


__all__ = [
    "PRClosed",
    "PRCommented",
    "PRLabeled",
    "PRMerged",
    "PRReviewSubmitted",
    "PRUnlabeled",
    "get_effect_types",
]
