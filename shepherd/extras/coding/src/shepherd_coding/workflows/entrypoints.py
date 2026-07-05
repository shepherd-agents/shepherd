"""Entrypoints for the PR review workflow.

These are the primary public API — thin wrappers that handle config
resolution, pipeline execution, and output formatting.

Config resolution uses ``resolve_config()`` which handles the full
lifecycle: cached YAML -> LLM inference -> defaults.
"""

from __future__ import annotations

import asyncio
import sys
from typing import Any

from shepherd.autoconfig import resolve_config

from .config import PRReviewConfig


def _check_sync_context(func_name: str) -> None:
    """Raise if called from within a running event loop."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return  # No loop — expected for sync callers
    msg = f"{func_name}() is a sync API and cannot be called from within a running event loop."
    raise RuntimeError(msg)


def _register_provider(scope: Any) -> None:
    """Register a discovered provider as the scope default."""
    from shepherd_runtime.registry import discover_providers

    providers = discover_providers()
    provider_cls = providers.get("claude") or providers.get("openai") or providers.get("mock")
    if provider_cls is None:
        msg = (
            "No LLM provider found. Install a shepherd provider package "
            "(e.g. shepherd-provider-claude) or set SHEPHERD_PROVIDER."
        )
        raise RuntimeError(msg)
    provider = provider_cls(name="pr-review", model="claude-haiku-4-5")
    scope.register_provider("default", provider, default=True)


def review_pr(
    pr_number: int,
    *,
    config: PRReviewConfig | None = None,
    scope: Any = None,
    quiet: bool = False,
) -> Any:
    """Review a pull request and return structured findings.

    Config resolution order:
        1. Explicit ``config`` parameter (if provided)
        2. Cached YAML (``.shepherd/pr_review.yaml``)
        3. LLM inference from workspace analysis
        4. Default values

    Args:
        pr_number: Pull request number to review.
        config: Review configuration. If None, resolved automatically.
        scope: Shepherd Scope for effect integration. Created automatically
            if None.
        quiet: If True, suppress terminal output.

    Returns:
        Completed PRReview task instance with summary, findings,
        approval, score, and stages attributes.
    """
    _check_sync_context("review_pr")
    resolved_config = resolve_config(PRReviewConfig, config)
    return asyncio.run(_run_pipeline(pr_number, resolved_config, scope, quiet))


async def _run_pipeline(
    pr_number: int,
    config: PRReviewConfig,
    scope: Any,
    quiet: bool,
) -> Any:
    """Run the PRReview pipeline."""
    from shepherd_runtime.scope import Scope

    from .formatter import format_review
    from .pipeline import PRReview

    parent_scope = scope
    if parent_scope is None:
        async with Scope(root=True) as auto_scope:
            _register_provider(auto_scope)
            result = await PRReview.arun(
                scope=auto_scope,
                pr_number=pr_number,
                config=config,
            )
            if not quiet:
                output = format_review(result)
                print(output, file=sys.stderr)  # noqa: T201
            return result
    else:
        result = await PRReview.arun(
            scope=parent_scope,
            pr_number=pr_number,
            config=config,
        )
        if not quiet:
            output = format_review(result)
            print(output, file=sys.stderr)  # noqa: T201
        return result


def review_pr_auto(
    pr_number: int,
    *,
    workspace_path: str = ".",
    persist: bool = True,
    force: bool = False,
    scope: Any = None,
    quiet: bool = False,
) -> Any:
    """Review a PR with auto-generated configuration from codebase analysis.

    Backward-compatible wrapper around ``review_pr()`` with ``resolve_config()``.
    New code should use ``review_pr()`` directly — it auto-resolves config
    through the same cached YAML -> LLM inference -> defaults chain.

    Args:
        pr_number: Pull request number to review.
        workspace_path: Path to the repository root for config inference.
        persist: If True (default), write inferred config to YAML for reuse.
        force: If True, re-infer even if cached config exists.
        scope: Shepherd Scope for effect integration.
        quiet: If True, suppress terminal output.

    Returns:
        Completed PRReview task instance.
    """
    _check_sync_context("review_pr_auto")
    resolved_config = resolve_config(
        PRReviewConfig,
        persist=persist,
        force=force,
    )
    return asyncio.run(_run_pipeline(pr_number, resolved_config, scope, quiet))
