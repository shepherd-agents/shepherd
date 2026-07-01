"""CritiqueRefineLoop — iterate critique-refine cycles until convergence."""

from __future__ import annotations

import shutil
from pathlib import Path

from pydantic import BaseModel, Field
from shepherd_contexts import WorkspaceRef  # noqa: TC002
from shepherd_runtime.task.authoring import Context, Input, Output, task
from shepherd_runtime.task.pipeline import OnError

from shepherd_authoring.checks import check_file_exists
from shepherd_authoring.models import CritiqueIssue, CritiqueOutput, IssueStatus
from shepherd_authoring.tasks.critique_documents import CritiqueDocuments
from shepherd_authoring.tasks.refine_documents import RefineDocuments

# Number of consecutive iterations with the same score before entering plateau mode
_PLATEAU_THRESHOLD = 2


def _append_refinement_log(
    log_path: Path,
    iteration: int,
    score: float,
    issues: list[CritiqueIssue],
    *,
    plateau_mode: bool = False,
) -> None:
    """Append an iteration entry to REFINEMENT-LOG.md."""
    mode_tag = " (triage)" if plateau_mode else ""
    issue_parts = []
    for issue in issues:
        status_tag = f"[{issue.status.value}] " if issue.status != IssueStatus.NEW else ""
        issue_parts.append(f"{status_tag}{issue.description}")

    entry = f"""
## Iteration {iteration}{mode_tag}
**Score**: {score}/10
**Issues**: {", ".join(issue_parts) if issue_parts else "None"}
"""
    existing = log_path.read_text() if log_path.exists() else "# Refinement Log\n"
    log_path.write_text(existing + entry)


def _save_version_snapshot(versions_dir: Path, iteration: int, document_paths: dict[str, str]) -> None:
    """Save a pre-modification backup of every document before refinement."""
    versions_dir.mkdir(parents=True, exist_ok=True)
    for name, path_str in document_paths.items():
        src = Path(path_str)
        if src.exists():
            dest = versions_dir / f"{name}.v{iteration}{src.suffix}"
            shutil.copy2(src, dest)


def _restore_version_snapshot(versions_dir: Path, iteration: int, document_paths: dict[str, str]) -> None:
    """Restore all documents from a snapshot."""
    for name, path_str in document_paths.items():
        src_path = Path(path_str)
        snapshot = versions_dir / f"{name}.v{iteration}{src_path.suffix}"
        if snapshot.exists():
            shutil.copy2(snapshot, path_str)


def _write_diagnostic(log_path: Path, trajectory: list[float], *, soft_converged: bool = False) -> None:
    """Append an exit diagnostic to the refinement log."""
    if soft_converged:
        status = "Yes (soft — plateau with no blocking issues)"
    else:
        status = "No (budget exhausted)"

    entry = f"""
## Diagnostic
**Score trajectory**: {" -> ".join(f"{s:.1f}" for s in trajectory)}
**Converged**: {status}
"""
    existing = log_path.read_text() if log_path.exists() else "# Refinement Log\n"
    log_path.write_text(existing + entry)


def _is_plateau(trajectory: list[float], threshold: int = _PLATEAU_THRESHOLD) -> bool:
    """Return True if the last `threshold` scores are identical."""
    if len(trajectory) < threshold:
        return False
    recent = trajectory[-threshold:]
    return all(s == recent[0] for s in recent)


def _has_no_blocking_issues(issues: list[CritiqueIssue]) -> bool:
    """Return True if no issues are new or unchanged (all resolved or partially resolved)."""
    return all(issue.status not in (IssueStatus.NEW, IssueStatus.UNCHANGED) for issue in issues)


@task
class CritiqueRefineLoop(BaseModel):
    """Iterate critique-refine cycles until convergence or budget exhaustion.

    Detects score plateaus and switches the critic to triage mode, where
    it evaluates whether prior issues have been addressed rather than
    restating them. Declares soft convergence when the score plateaus
    and no blocking issues remain.
    """

    # Inputs
    document_paths: Input(dict[str, str]) = Field(description="name -> path mapping")
    principles: Input(list[str]) = Field(description="Guiding principles")
    max_iterations: Input(int) = Field(default=5)
    target_score: Input(float) = Field(default=8.0)
    workspace_path: Input(str) = Field(description="Workspace directory path")

    # Context
    workspace: Context[WorkspaceRef]

    # Outputs
    final_score: Output(float) = Field(default=0.0)
    iterations_used: Output(int) = Field(default=0)
    converged: Output(bool) = Field(default=False)

    def execute(self) -> None:
        workspace_dir = Path(self.workspace_path)
        versions_dir = workspace_dir / ".versions"
        log_path = workspace_dir / "REFINEMENT-LOG.md"
        trajectory: list[float] = []
        reasoning_ctx: str | None = None
        prior_issue_descriptions: list[str] = []

        self._validate_preconditions()

        for iteration in range(1, self.max_iterations + 1):
            plateau_mode = _is_plateau(trajectory)

            result = self._run_iteration(
                iteration,
                versions_dir,
                log_path,
                trajectory,
                reasoning_ctx,
                prior_issue_descriptions,
                plateau_mode=plateau_mode,
            )
            if result is None:
                return  # converged or halted inside _run_iteration

            _score, reasoning_ctx, prior_issue_descriptions = result

        # Budget exhausted
        self.final_score = trajectory[-1] if trajectory else 0.0
        self.iterations_used = self.max_iterations
        self.converged = False
        _write_diagnostic(log_path, trajectory)

    def _validate_preconditions(self) -> None:
        """Verify all referenced documents exist before entering the loop."""
        for name, path_str in self.document_paths.items():
            if not check_file_exists(Path(path_str)):
                raise FileNotFoundError(f"Document '{name}' not found at: {path_str}")

    def _run_iteration(
        self,
        iteration: int,
        versions_dir: Path,
        log_path: Path,
        trajectory: list[float],
        reasoning_ctx: str | None,
        prior_issue_descriptions: list[str],
        *,
        plateau_mode: bool = False,
    ) -> tuple[float, str, list[str]] | None:
        """Run one critique-refine cycle.

        Returns ``(score, reasoning_ctx, issue_descriptions)`` to continue
        looping, or ``None`` if the loop should exit (converged or halted).
        """
        # 1. Critique
        critique = self.run_stage_sync(
            f"critique_{iteration}",
            CritiqueDocuments,
            document_paths=self.document_paths,
            principles=self.principles,
            prior_reasoning=reasoning_ctx,
            prior_issues=prior_issue_descriptions,
            plateau=plateau_mode,
        )
        score = critique.score if critique.score is not None else 0.0
        issues: list[CritiqueIssue] = critique.issues if critique.issues is not None else []
        reasoning_ctx = critique.reasoning_context if critique.reasoning_context is not None else ""
        trajectory.append(score)

        # Extract issue descriptions for next iteration's prior_issues
        current_issue_descriptions = [i.description for i in issues]

        # 2. Log
        _append_refinement_log(log_path, iteration, score, issues, plateau_mode=plateau_mode)

        # 3. Hard convergence — target score met
        if score >= self.target_score:
            self.final_score = score
            self.iterations_used = iteration
            self.converged = True
            return None

        # 4. Soft convergence — plateau with no blocking issues
        if plateau_mode and _has_no_blocking_issues(issues):
            self.final_score = score
            self.iterations_used = iteration
            self.converged = True
            _write_diagnostic(log_path, trajectory, soft_converged=True)
            return None

        # 5. Snapshot before modification
        _save_version_snapshot(versions_dir, iteration, self.document_paths)

        # 6. Refine — recover from LLM failures by restoring snapshot
        critique_output = CritiqueOutput(
            score=score,
            issues=issues,
            suggestions=critique.suggestions if critique.suggestions is not None else [],
            reasoning_context=reasoning_ctx,
        )
        refined = self.run_stage_sync(
            f"refine_{iteration}",
            RefineDocuments,
            on_error=OnError.skip,
            document_paths=self.document_paths,
            critique=critique_output,
            principles=self.principles,
        )
        if refined is None:
            _append_refinement_log(
                log_path,
                iteration,
                score,
                [CritiqueIssue(description="RefineDocuments failed; restoring snapshot")],
            )
            _restore_version_snapshot(versions_dir, iteration, self.document_paths)
            return score, reasoning_ctx, current_issue_descriptions

        # 7. Postcondition: verify documents survived refinement
        destroyed = [p for p in self.document_paths.values() if not check_file_exists(Path(p))]
        if destroyed:
            destroy_issues = [CritiqueIssue(description=f"Document destroyed: {p}") for p in destroyed]
            _append_refinement_log(log_path, iteration, score, destroy_issues)
            _restore_version_snapshot(versions_dir, iteration, self.document_paths)
            self.final_score = score
            self.iterations_used = iteration
            self.converged = False
            return None

        return score, reasoning_ctx, current_issue_descriptions


__all__ = ["CritiqueRefineLoop"]
