"""Real LLM smoke test for configure_pr_review.

Run with: SHEPHERD_RUN_LLM_TESTS=1 uv run pytest tests/integration/test_real_inference.py -v

Tranche 7 migration note (DECISIONS D5): the class-form
``ConfigurePRReview`` was retired in favor of function-form
``configure_pr_review``. This test uses the function-form invocation
shape.
"""

import os
from pathlib import Path

import pytest
from shepherd_coding.workflows.pr_review.config import PRReviewConfig

_SKIP_REASON = "Set SHEPHERD_RUN_LLM_TESTS=1 to run real LLM tests"


@pytest.mark.integration
@pytest.mark.skipif(
    not os.environ.get("SHEPHERD_RUN_LLM_TESTS"),
    reason=_SKIP_REASON,
)
async def test_real_inference_produces_useful_config() -> None:
    """configure_pr_review against this repo should produce a non-trivial config."""
    from shepherd_coding.tasks import configure_pr_review
    from shepherd_contexts.workspace.ref import WorkspaceRef
    from shepherd_runtime.nucleus.workspace import reset_workspace_for_tests
    from shepherd_runtime.registry import discover_providers

    from shepherd import workspace

    providers = discover_providers()
    provider_cls = providers.get("claude") or providers.get("openai")
    if provider_cls is None:
        pytest.skip("No real provider available")

    reset_workspace_for_tests()
    try:
        ws = workspace(model=provider_cls(name="real-test"), root=str(Path.cwd()))
        with ws.scope as scope:
            scope.bind("workspace", WorkspaceRef.readonly(str(Path.cwd())))
            config = await configure_pr_review()
    finally:
        reset_workspace_for_tests()

    assert isinstance(config, PRReviewConfig)

    # Quality: the LLM should have found something useful
    assert len(config.guidelines) > 20, "Guidelines should be substantive"
    assert len(config.focus_areas) >= 2, "Should identify at least 2 focus areas"
    assert config.max_comments >= 1, "max_comments must satisfy ge=1 constraint"

    # Infrastructure fields must be null (mitigation strategy works)
    assert config.repo is None, "LLM should not populate repo"
    assert config.github_token is None, "LLM should not populate github_token"
    assert config.clone_url is None, "LLM should not populate clone_url"

    # Verify should be populated if CI config exists (this repo has CI)
    if config.verify is not None:
        assert len(config.verify.test_command) > 0
