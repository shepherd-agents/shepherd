"""PRReview pipeline task — multi-stage PR review workflow.

Orchestrates: FetchPR → CheckoutPR → TriagePR → [VerifyPR] → ReviewPR
"""

from __future__ import annotations

import shutil
import tempfile
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field
from shepherd_contexts.workspace.ref import WorkspaceRef
from shepherd_runtime.task.authoring import Input, Output, task
from shepherd_runtime.task.pipeline import OnError

from shepherd_coding.contexts import GitHubContext
from shepherd_coding.findings import CodeFinding, review_finding_to_code_finding
from shepherd_coding.models import PRDetails  # noqa: TC001
from shepherd_coding.tasks.checkout_pr import CheckoutPR
from shepherd_coding.tasks.fetch_pr import FetchPR
from shepherd_coding.tasks.review_pr import ReviewPR
from shepherd_coding.tasks.triage_pr import TriagePR

from .config import PRReviewConfig
from .diff_format import format_diff_for_review


@task
class PRReview(BaseModel):
    """Multi-stage PR review pipeline.

    Fetches PR data, checks out the code, triages for risk,
    optionally runs build/tests, and produces a structured review.

    The pipeline returns findings for programmatic consumption.
    Use ``format_review()`` to render findings to the terminal.
    """

    pr_number: Input(int) = Field(description="Pull request number to review")
    config: Input[PRReviewConfig | None] = Field(default=None, description="Review configuration")

    summary: Output(str) = ""
    findings: Output(list[CodeFinding]) = Field(default=[])
    approval: Output(Literal["APPROVE", "REQUEST_CHANGES", "COMMENT"]) = "COMMENT"
    score: Output(float) = 0.0

    async def execute(self) -> None:
        scope = self.scope
        config = self.config or PRReviewConfig()
        checkout_dir: str | None = None

        try:
            # === Stage 1: Fetch PR details ===
            fetch = await self.run_stage(
                "fetch",
                FetchPR,
                on_error=OnError.fatal,
                pr_number=self.pr_number,
                repo=config.repo,
            )
            pr_details: PRDetails = fetch.details

            # === Stage 2: Checkout code ===
            checkout_dir = tempfile.mkdtemp(prefix="pr-review-")
            clone_url = config.clone_url or pr_details.clone_url
            head_sha = pr_details.head_sha

            checkout = await self.run_stage(
                "checkout",
                CheckoutPR,
                on_error=OnError.fatal,
                head_sha=head_sha,
                clone_url=clone_url,
                workspace_dir=checkout_dir,
            )

            # Bind contexts to pipeline scope for downstream stages
            ws_ref = WorkspaceRef.readonly(checkout.workspace_path)
            scope.bind("workspace", ws_ref)

            github_ctx = GitHubContext(
                repo=config.repo,
                token=config.github_token,
            )
            scope.bind("github", github_ctx)

            # === Stage 3: Triage ===
            triage = await self.run_stage(
                "triage",
                TriagePR,
                retry=1,
                on_error=OnError.default(category="unknown", risk_level="medium"),
                details=pr_details,
            )

            # === Stage 4: Verify (optional) ===
            verification_results: str | None = None
            if config.verify and triage.category != "docs":
                # VerifyPR is imported lazily — it may not exist yet
                try:
                    from shepherd_coding.tasks.verify_pr import VerifyPR

                    verify = await self.run_stage(
                        "verify",
                        VerifyPR,
                        on_error=OnError.skip,
                        pr_details=pr_details,
                        setup_commands=config.verify.setup_commands,
                        build_command=config.verify.build_command,
                        test_command=config.verify.test_command,
                    )
                    if verify is not None:
                        verification_results = verify.failure_analysis
                except ImportError:
                    pass  # VerifyPR not implemented yet — skip

            # === Stage 5: Review ===
            diff_text = format_diff_for_review(
                pr_details,
                skip_patterns=config.file_patterns_to_skip,
            )

            review = await self.run_stage(
                "review",
                ReviewPR,
                retry=2,
                on_error=OnError.continue_with(
                    summary="Review failed",
                    findings=[],
                    approval="COMMENT",
                    score=0.0,
                ),
                details=pr_details,
                diff_text=diff_text,
                focus_areas=config.focus_areas,
                guidelines=config.guidelines or None,
                verification_results=verification_results,
            )

            # Populate pipeline outputs from the review stage.
            # ReviewPR produces ReviewFinding — convert to CodeFinding.
            self.summary = review.summary
            raw_findings = review.findings or []
            self.findings = [review_finding_to_code_finding(f) for f in raw_findings]
            self.approval = review.approval
            self.score = review.score

        finally:
            # Cleanup checkout directory
            if checkout_dir and Path(checkout_dir).exists():
                shutil.rmtree(checkout_dir, ignore_errors=True)
