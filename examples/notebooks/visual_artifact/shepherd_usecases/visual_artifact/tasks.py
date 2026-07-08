"""Provider-owned task declarations for the visual-artifact notebooks."""

from __future__ import annotations

from shepherd_runtime.nucleus import GitRepo  # noqa: TC002 - task registration resolves this at runtime

STATIC_ARTIFACT_TASK_REF = "shepherd_usecases.visual_artifact.tasks.static_artifact_task"
LIVE_ARTIFACT_TASK_REF = "shepherd_usecases.visual_artifact.tasks.live_artifact_task"
LIVE_REVIEW_TASK_REF = "shepherd_usecases.visual_artifact.tasks.live_review_task"


def static_artifact_task(
    repo: GitRepo,
    *,
    output_path: str,
    output_text: str | None = None,
    output_content: object | None = None,
    **artifact_refs: object,
) -> object:
    """Declare the static visual-artifact task executed by the provider runtime."""
    raise RuntimeError("static_artifact_task is provider-owned; use launch.run_static() from the notebooks.")


def live_artifact_task(
    repo: GitRepo,
    *,
    prompt: str,
    variant: str,
    instruction: str,
    output_path: str = "index.html",
) -> object:
    """Create one self-contained HTML visual artifact.

    Use the user prompt, variant name, and instruction to produce a polished
    single-file HTML/CSS artifact. Write the final document to `output_path`.
    Do not write provider logs, transcripts, credentials, or scratch files.
    """
    raise RuntimeError("live_artifact_task is provider-owned; use launch.run_claude_artifact().")


def live_review_task(
    repo: GitRepo,
    *,
    prompt: str,
    output_path: str = "verdict.json",
    **candidate_refs: object,
) -> object:
    """Review cited visual artifacts and write a JSON verdict.

    Inspect the candidate artifact references materialized by the runtime.
    Write a JSON object to `output_path` with:
    `selected`, the chosen candidate id; and `candidates`, a list of objects
    with `id`, `verdict`, and `issues` fields. Prefer artifacts that match the
    prompt and clearly show the update path descending toward the minimum.
    """
    raise RuntimeError("live_review_task is provider-owned; use launch.run_claude_review().")


__all__ = [
    "LIVE_ARTIFACT_TASK_REF",
    "LIVE_REVIEW_TASK_REF",
    "STATIC_ARTIFACT_TASK_REF",
    "live_artifact_task",
    "live_review_task",
    "static_artifact_task",
]
