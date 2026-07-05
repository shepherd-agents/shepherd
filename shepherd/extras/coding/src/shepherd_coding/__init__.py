"""Shepherd Coding - GitHub integration, code review, and quality gate tasks."""

from __future__ import annotations

from shepherd_core.package import package

__version__ = "0.2.0"

# Unified findings model
# Context
from shepherd_coding.contexts import GitHubContext

# Effects
from shepherd_coding.contexts.effects import (
    PRClosed,
    PRCommented,
    PRLabeled,
    PRMerged,
    PRReviewSubmitted,
    PRUnlabeled,
)
from shepherd_coding.findings import (
    CATEGORY_PRIORITY,
    CodeFinding,
    Confidence,
    Severity,
    Source,
    UnifiedFixRecord,
    format_findings_for_llm,
    issue_to_code_finding,
    review_finding_to_code_finding,
)

# Models
from shepherd_coding.models import (
    PRAuthor,
    PRCommit,
    PRDetails,
    PRFile,
    PRLabel,
    PRReview,
    ReviewFinding,
)

# Tasks
from shepherd_coding.tasks import (
    FetchPR,
    PRDescriptionResult,
    Review,
    ReviewPR,
    SummaryResult,
    Triage,
    TriagePR,
    generate_pr_description,
    run_linter,
    summarize,
)

# Utilities
from shepherd_coding.utils import (
    GitHubRepoError,
    GitHubTokenError,
    get_github_token,
    get_pr_details,
    get_repo_from_git,
    parse_repo_from_url,
)


@package(
    name="coding",
    version=__version__,
    tasks=["shepherd_coding.tasks"],
    contexts=["shepherd_coding.contexts"],
    effects=["shepherd_coding.contexts.effects"],
)
def coding() -> None:
    """GitHub integration, code review, and quality gate tasks."""


__all__ = [
    "CATEGORY_PRIORITY",
    "CodeFinding",
    "Confidence",
    "FetchPR",
    "GitHubContext",
    "GitHubRepoError",
    "GitHubTokenError",
    "PRAuthor",
    "PRClosed",
    "PRCommented",
    "PRCommit",
    "PRDescriptionResult",
    "PRDetails",
    "PRFile",
    "PRLabel",
    "PRLabeled",
    "PRMerged",
    "PRReview",
    "PRReviewSubmitted",
    "PRUnlabeled",
    "Review",
    "ReviewFinding",
    "ReviewPR",
    "Severity",
    "Source",
    "SummaryResult",
    "Triage",
    "TriagePR",
    "UnifiedFixRecord",
    "__version__",
    "coding",
    "format_findings_for_llm",
    "generate_pr_description",
    "get_github_token",
    "get_pr_details",
    "get_repo_from_git",
    "issue_to_code_finding",
    "parse_repo_from_url",
    "review_finding_to_code_finding",
    "run_linter",
    "summarize",
]
