"""PrePushQualityGate — multi-stage quality pipeline.

Orchestrates: MechanicalFix → Diagnostics → Validation → FixLoop → FinalGate → PRDescription

Phase coverage by mode:
  fast:     Phase 1 + Phase 5 + Phase 6 (template)
  standard: Phase 1 + Phase 2 (programmatic) + Phase 5 + Phase 6 (LLM)
  full:     All six phases
"""

from __future__ import annotations

import logging
import subprocess
import sys
from typing import Literal

logger = logging.getLogger(__name__)

from pydantic import BaseModel, Field
from shepherd_contexts.workspace.ref import WorkspaceRef
from shepherd_core import Infer  # noqa: TC002 (runtime: Pydantic resolves Infer annotations)
from shepherd_runtime.context.sandbox import GitWorktreeSandbox
from shepherd_runtime.task.authoring import Input, Output, task
from shepherd_runtime.task.pipeline import OnError, Stage

from shepherd_coding.findings import (
    CATEGORY_PRIORITY,
    CodeFinding,
    Source,
    format_findings_for_llm,
)
from shepherd_coding.tasks.analyze_code import AnalyzeCode
from shepherd_coding.tasks.generate_fixes import GenerateFixes
from shepherd_coding.tasks.generate_pr_description import GeneratePRDescription
from shepherd_coding.tasks.run_formatter import RunFormatter
from shepherd_coding.tasks.run_linter import RunLinter
from shepherd_coding.tasks.run_tests import RunTests
from shepherd_coding.tasks.run_type_checker import RunTypeChecker
from shepherd_coding.tasks.validate_issues import ValidateIssues

from .models import (
    FixRecord,
    QualityGateVerdict,
    ToolRunResult,
    net_progress,
)

# =============================================================================
# Config
# =============================================================================


class QualityGateConfig(BaseModel):
    """Configuration for the quality gate pipeline.

    Infer() fields are automatically filled by LLM workspace analysis when
    using ``resolve_config(QualityGateConfig)``.
    """

    # Inferable fields — descriptions carry derivation rules
    mode: Infer(Literal["fast", "standard", "full"]) = Field(
        default="full",
        description=(
            "Pipeline mode based on project maturity. "
            "'fast' for projects with no CI or minimal tooling. "
            "'standard' for projects with CI that runs lint + type-check + tests. "
            "'full' for projects that want LLM-assisted code analysis."
        ),
    )
    base_ref: Infer(str) = Field(
        default="",
        description=(
            "Branch to diff against. Check the repository's default branch "
            "(usually 'main' or 'master'). Empty = auto-discover from the "
            "head branch of the most recently opened PR."
        ),
    )
    mypy_target: Infer(str) = Field(
        default=".",
        description=(
            "mypy target path. Extract from CI config (.github/workflows/*.yml "
            "or Makefile) — look for 'mypy <target>'. If CI doesn't run mypy, "
            "set to empty string (empty = skip mypy). Do NOT default to '.' — "
            "running mypy on the project root often fails on non-Python dirs."
        ),
    )
    test_paths: Infer(list[str]) = Field(
        default_factory=list,
        description=(
            "pytest paths and flags from CI config. Include exact paths and "
            "flags, e.g. ['packages/', '-m', 'not container and not e2e']. "
            "Include --ignore=<path> flags directly in this list. "
            "Empty = run pytest with no path args."
        ),
    )
    include_paths: Infer(list[str]) = Field(
        default_factory=list,
        description=(
            "Only analyze files matching these path prefixes. "
            "Empty = all files not excluded. Derive from project structure."
        ),
    )
    exclude_paths: Infer(list[str]) = Field(
        default_factory=list,
        description=(
            "Skip files matching these path prefixes. Common exclusions: "
            "'design/', 'spikes/', 'docs/', 'vendor/'. Derive from "
            ".gitignore and CI config skip patterns."
        ),
    )

    # Non-inferable fields — runtime configuration
    workspace_path: str = "."
    model: str = "claude-haiku-4-5"
    max_fix_iterations: int = 3
    max_mechanical_iterations: int = 2


def _log(msg: str) -> None:
    """Emit a timestamped progress line to stderr."""
    from datetime import datetime, timezone

    ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
    print(f"[{ts}] {msg}", file=sys.stderr, flush=True)  # noqa: T201


# Issue priority for the fix loop
# Issue priority — uses string keys (from findings.CATEGORY_PRIORITY)
_CATEGORY_PRIORITY = CATEGORY_PRIORITY

_ISSUE_BATCH_SIZE = 8


# =============================================================================
# Pipeline @task
# =============================================================================


@task
class PrePushQualityGate(BaseModel):
    """Pre-push quality gate pipeline.

    Catches lint, format, type, and test failures before CI. In full mode,
    adds LLM-powered analysis, concern validation, and automated fixes.
    Produces a PR-ready verdict and description.
    """

    config: Input(QualityGateConfig) = Field(default=None, description="Pipeline configuration")

    # Outputs
    verdict: Output(QualityGateVerdict) = "needs_human_review"
    pr_title: Output(str) = ""
    pr_body: Output(str) = ""
    auto_fixes: Output(list[FixRecord]) = Field(default=[])
    unresolved: Output(list[CodeFinding]) = Field(default=[])
    changed_files: Output(list[str]) = Field(default=[])
    fix_branch: Output(str) = Field(
        default="",
        description="Git branch name containing committed fixes (e.g., "
        "'quality-gate/fixes-20260324-1200'). Empty if no fixes were generated. "
        "Review with: git log <fix_branch> --oneline. "
        "Apply with: git merge <fix_branch>. "
        "Discard with: git branch -D <fix_branch>.",
    )

    async def execute(self) -> None:
        scope = self.scope
        config = self.config or QualityGateConfig()

        # Resolve base ref: explicit > auto-discover from most recent PR > "main"
        base_ref = config.base_ref or _discover_base_ref(config.workspace_path)

        # Gather change information, filtered by include/exclude paths
        all_changed = _get_changed_files(config.workspace_path, base_ref)
        changed_files = _filter_paths(all_changed, config.include_paths, config.exclude_paths)
        self.changed_files = changed_files
        commit_log = _get_commit_log(config.workspace_path, base_ref)

        _log(f"Quality gate starting: mode={config.mode}, {len(changed_files)} changed files, base_ref={base_ref}")

        # Bind workspace for LLM tasks that need Context(WorkspaceRef)
        ws_ref = WorkspaceRef.readonly(config.workspace_path)
        scope.bind("workspace", ws_ref)

        all_tool_results: list[ToolRunResult] = []
        all_issues: list[CodeFinding] = []

        # Phase 1+5: Mechanical fix loop → gate check
        gate_passed, gate_mypy, gate_pytest = await self._run_mechanical_loop(
            config,
            changed_files,
            all_tool_results,
        )
        all_tool_results.append(gate_mypy.model_copy(update={"tool": "mypy (gate)"}))
        all_tool_results.append(gate_pytest.model_copy(update={"tool": "pytest (gate)"}))
        all_issues.extend(gate_mypy.findings)
        all_issues.extend(gate_pytest.findings)

        # =================================================================
        # Phase 2: LLM Diagnostics — full mode only
        #
        # Diff is computed on-demand (fresh after Phase 1 mechanical fixes).
        # Programmatic diagnostics are already captured from the gate check.
        # =================================================================
        if config.mode == "full":
            _log(f"Phase 2: LLM analyzers (4 categories, batches of 2) — {len(all_issues)} programmatic issues so far")
            diff_text = _get_diff(config.workspace_path, base_ref, changed_files)
            llm_findings = await self._run_llm_analyzers(scope, diff_text)
            _log(f"  LLM analyzers found {len(llm_findings)} issues")
            all_issues.extend(llm_findings)

        # =================================================================
        # Phase 3: Concern Validation — full mode only
        # =================================================================
        if config.mode == "full":
            llm_count = sum(1 for i in all_issues if i.source == Source.LLM)
            _log(f"Phase 3: Concern validation ({llm_count} LLM issues, batches of {_ISSUE_BATCH_SIZE})")
            all_issues = await self._validate_concerns(scope, all_issues)
            _log(f"  {len(all_issues)} issues remain after validation")

        # =================================================================
        # Phase 4: Fix Loop — full mode only
        #
        # The fix task gets write + bash access to a worktree. It edits
        # files directly and runs verification commands to self-check.
        # The pipeline commits or rolls back based on the outcome.
        # =================================================================
        if config.mode == "full" and all_issues:
            _log(f"Phase 4: Fix loop ({len(all_issues)} issues, up to {config.max_fix_iterations} iterations)")
            fixed, remaining = await self._fix_loop(scope, config, all_issues)
            self.auto_fixes = fixed
            all_issues = remaining
            _log(f"  {len(fixed)} fixes accepted, {len(remaining)} unresolved")

        self.unresolved = all_issues
        self.verdict = "ready" if gate_passed else "needs_human_review"

        # =================================================================
        # Phase 6: PR Description
        # =================================================================
        tool_summary = "\n".join(
            f"  {t.tool}: {'SKIP' if t.skipped else 'PASS' if t.passed else 'FAIL'}"
            + (f" ({t.skip_reason})" if t.skipped else f" ({len(t.findings)} issues)")
            for t in all_tool_results
        )
        unresolved_summary = "\n".join(f"  [{i.severity.value}] {i.description}" for i in self.unresolved[:20])

        _log(f"Phase 6: PR description ({'template' if config.mode == 'fast' else 'LLM'})")
        if config.mode == "fast":
            self.pr_title, self.pr_body = _template_description(
                changed_files,
                commit_log,
                self.verdict,
                all_tool_results,
                self.unresolved,
                self.auto_fixes,
            )
        else:
            # Compute diff on-demand for PR description (fresh, reflects all fixes)
            pr_diff = _get_diff(config.workspace_path, base_ref, changed_files)
            per_file = _split_diff_per_file(pr_diff)
            condensed_for_pr = _condense_diff(per_file, max_total_chars=15000)

            pr_desc = await self.run_stage(
                "pr_description",
                GeneratePRDescription,
                retry=1,
                on_error=OnError.continue_with(pr_title="feat: update", pr_body="(generation failed)"),
                diff_text=condensed_for_pr,
                commit_log=commit_log[:3000],
                changed_files=changed_files,
                verdict=self.verdict,
                tool_summary=tool_summary,
                unresolved_summary=unresolved_summary,
            )
            self.pr_title = pr_desc.pr_title or "feat: update"
            self.pr_body = pr_desc.pr_body or "(no description)"

    # =================================================================
    # Phase 1+5 helper: Mechanical fix loop → gate check
    # =================================================================

    async def _run_mechanical_loop(
        self,
        config: QualityGateConfig,
        changed_files: list[str],
        all_tool_results: list[ToolRunResult],
    ) -> tuple[bool, ToolRunResult, ToolRunResult]:
        """Run lint/format with auto-fix, then check mypy + pytest.

        Loops if mechanical fixes might resolve gate failures, bounded
        by ``config.max_mechanical_iterations``. Returns
        ``(gate_passed, gate_mypy_result, gate_pytest_result)``.
        """
        gate_passed = False
        gate_mypy = ToolRunResult(tool="mypy", passed=True, skipped=True, skip_reason="not run")
        gate_pytest = ToolRunResult(tool="pytest", passed=True, skipped=True, skip_reason="not run")

        for mech_iter in range(max(config.max_mechanical_iterations, 1)):
            iter_suffix = f"_iter{mech_iter}" if mech_iter > 0 else ""

            _log(f"Phase 1: Mechanical fix pass (iteration {mech_iter})")
            lint = await self.run_stage(
                f"lint{iter_suffix}",
                RunLinter,
                on_error=OnError.continue_with(result=ToolRunResult(tool="ruff-check", passed=False)),
                workspace_path=config.workspace_path,
                files=changed_files,
                fix=True,
            )
            lint_result = _get_tool_result(lint, "ruff-check")
            if mech_iter == 0:
                all_tool_results.append(lint_result)

            fmt = await self.run_stage(
                f"format{iter_suffix}",
                RunFormatter,
                on_error=OnError.continue_with(result=ToolRunResult(tool="ruff-format", passed=False)),
                workspace_path=config.workspace_path,
                files=changed_files,
            )
            fmt_result = _get_tool_result(fmt, "ruff-format")
            if mech_iter == 0:
                all_tool_results.append(fmt_result)

            _log(
                f"  lint: {'PASS' if lint_result.passed else 'FAIL'}, format: {'PASS' if fmt_result.passed else 'FAIL'}"
            )
            _log("Phase 5: Gate check (mypy + pytest)")
            if config.mypy_target:
                gate_tc = await self.run_stage(
                    f"gate_type_check{iter_suffix}",
                    RunTypeChecker,
                    on_error=OnError.continue_with(result=ToolRunResult(tool="mypy", passed=False)),
                    workspace_path=config.workspace_path,
                    target=config.mypy_target,
                )
                gate_mypy = _get_tool_result(gate_tc, "mypy")
            else:
                gate_mypy = ToolRunResult(tool="mypy", passed=True, skipped=True, skip_reason="mypy_target is empty")

            gate_tests = await self.run_stage(
                f"gate_tests{iter_suffix}",
                RunTests,
                on_error=OnError.continue_with(result=ToolRunResult(tool="pytest", passed=False)),
                workspace_path=config.workspace_path,
                test_paths=config.test_paths,
            )
            gate_pytest = _get_tool_result(gate_tests, "pytest")

            gate_passed = all(tr.passed or tr.skipped for tr in [gate_mypy, gate_pytest])
            _log(
                f"  mypy: {'PASS' if gate_mypy.passed else 'FAIL'}, "
                f"pytest: {'PASS' if gate_pytest.passed else 'FAIL'} → gate {'PASSED' if gate_passed else 'FAILED'}"
            )

            if gate_passed:
                break
            if lint_result.skipped and fmt_result.skipped:
                break
            if lint_result.passed and fmt_result.passed:
                break

        return gate_passed, gate_mypy, gate_pytest

    # =================================================================
    # Phase 2 helper: Parallel LLM analyzer fan-out
    # =================================================================

    async def _run_llm_analyzers(self, scope: object, diff_text: str) -> list[CodeFinding]:
        """Run 4 LLM analyzers in batches of 2 via run_stages_parallel."""
        per_file = _split_diff_per_file(diff_text)
        condensed = _condense_diff(per_file, max_total_chars=30000)

        analyzer_configs = [
            (
                "doc_gaps",
                "Check for stale docstrings, missing documentation on new public APIs, and parameter mismatches.",
            ),
            (
                "correctness",
                "Check for logic errors, unhandled edge cases, off-by-one errors, and silent failure paths.",
            ),
            (
                "consistency",
                "Check that new code follows existing naming conventions, error-handling patterns, and module structure.",
            ),
            (
                "coverage_gap",
                "Check for new code paths that lack test coverage, especially error paths and boundary conditions.",
            ),
        ]

        stages = [
            Stage(
                f"analyze_{category}",
                AnalyzeCode,
                {"diff_text": condensed, "category": category, "focus_prompt": focus_prompt},
                on_error=OnError.skip,
            )
            for category, focus_prompt in analyzer_configs
        ]

        _log(f"  Running {len(stages)} analyzers (max_concurrency=2)")
        results = await self.run_stages_parallel(*stages, max_concurrency=2)

        all_findings: list[CodeFinding] = []
        for result in results:
            if result is not None:
                all_findings.extend(result.findings or [])

        return all_findings

    # =================================================================
    # Phase 3 helper: Batched concern validation
    # =================================================================

    async def _validate_concerns(self, scope: object, issues: list[CodeFinding]) -> list[CodeFinding]:
        """Validate LLM-sourced issues in batches, keep programmatic issues as-is."""
        programmatic = [i for i in issues if i.source == Source.PROGRAMMATIC]
        llm_issues = [i for i in issues if i.source == Source.LLM]

        if not llm_issues:
            return issues

        confirmed: list[CodeFinding] = []

        for batch_start in range(0, len(llm_issues), _ISSUE_BATCH_SIZE):
            batch = llm_issues[batch_start : batch_start + _ISSUE_BATCH_SIZE]
            _log(f"  Validating batch: issues {batch_start + 1}-{batch_start + len(batch)} of {len(llm_issues)}")

            issues_text = _format_issues_for_llm(batch)

            result = await self.run_stage(
                f"validate_batch_{batch_start}",
                ValidateIssues,
                on_error=OnError.skip,
                issues_text=issues_text,
            )

            if result is None:
                confirmed.extend(batch)  # Can't validate → treat as confirmed
            else:
                verdicts = result.verdicts or []
                verdict_map = {v.issue_number: v for v in verdicts}

                for idx, issue in enumerate(batch, start=1):
                    v = verdict_map.get(idx)
                    if v is None or v.verdict != "dropped":
                        if v and v.suggested_fix_approach:
                            issue = issue.model_copy(update={"suggested_fix": v.suggested_fix_approach})
                        confirmed.append(issue)
                    else:
                        _log(f"    Dropped issue {batch_start + idx}: {issue.description[:60]}")

        return programmatic + confirmed

    # =================================================================
    # Phase 4 helper: Batched fix loop with worktree isolation
    # =================================================================

    async def _fix_loop(
        self,
        scope: object,
        config: QualityGateConfig,
        issues: list[CodeFinding],
    ) -> tuple[list[FixRecord], list[CodeFinding]]:
        """Iterative fix loop with workspace-direct execution.

        The fix task gets write + bash access to a git worktree. It edits
        files directly and runs verification commands to self-check. The
        pipeline commits successful batches and rolls back failures.
        """
        auto_fixes: list[FixRecord] = []
        remaining = list(issues)

        remaining.sort(key=lambda i: _CATEGORY_PRIORITY.get(i.category, 99))

        from datetime import datetime, timezone

        timestamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M")
        branch_name = f"quality-gate/fixes-{timestamp}"

        sandbox = GitWorktreeSandbox(source_repo=config.workspace_path)
        sandbox.setup()

        wt_repo = sandbox._worktree_repo_obj
        wt_repo.git.checkout("-b", branch_name)

        verify_cmd = _build_verify_command(config)

        try:
            for iteration in range(config.max_fix_iterations):
                if not remaining:
                    break

                _log(f"  Fix iteration {iteration}: {len(remaining)} issues remaining")
                before_count = len(remaining)
                still_remaining: list[CodeFinding] = []

                for batch_start in range(0, len(remaining), _ISSUE_BATCH_SIZE):
                    batch = remaining[batch_start : batch_start + _ISSUE_BATCH_SIZE]
                    _log(f"    Fix batch: issues {batch_start + 1}-{batch_start + len(batch)}")

                    issues_text = _format_issues_for_llm(batch)

                    # Fork scope and override "workspace" to point at the
                    # writable worktree.  fork() inherits the parent's readonly
                    # binding, so we use update_context to replace it.
                    # After merge, restore the original binding so the
                    # temporary worktree ref doesn't persist on the parent.
                    original_ws = scope.get_context("workspace")
                    fork = scope.fork()
                    ws_ref = WorkspaceRef.from_path(str(sandbox.path)).with_bash()
                    fork.update_context("workspace", ws_ref)

                    try:
                        result = await GenerateFixes.arun(
                            scope=fork,
                            issues_text=issues_text,
                            verify_command=verify_cmd,
                        )
                        scope.merge(fork)
                        scope.update_context("workspace", original_ws)

                        fixes_applied = result.fixes_applied or []
                        tests_passed = result.tests_passed

                        # Only count fixes where the LLM actually changed files
                        has_changes = bool(
                            wt_repo.git.diff() or wt_repo.git.diff("--cached") or wt_repo.untracked_files
                        )

                        if fixes_applied and has_changes:
                            wt_repo.git.add("-A")

                            summary = result.summary or ""
                            commit_msg = (
                                f"fix: {summary[:60]}\n\n"
                                f"Issues fixed: {fixes_applied}\n"
                                f"Verified: {tests_passed}\n"
                                f"Auto-generated by quality gate pipeline"
                            )
                            wt_repo.git.commit("-m", commit_msg)

                            fixed_set = set(fixes_applied)
                            for idx, finding in enumerate(batch, start=1):
                                if idx in fixed_set:
                                    auto_fixes.append(FixRecord(finding=finding, verified=tests_passed))
                                else:
                                    still_remaining.append(finding)
                        else:
                            # LLM claimed fixes but made no changes, or no fixes at all
                            still_remaining.extend(batch)

                    except Exception:  # noqa: BLE001
                        logger.warning(
                            "Fix generation failed for batch of %d issues; rolling back",
                            len(batch),
                            exc_info=True,
                        )
                        fork.discard()
                        # Roll back any uncommitted changes in the worktree
                        wt_repo.git.checkout("--", ".")
                        wt_repo.git.clean("-fd")
                        still_remaining.extend(batch)

                remaining = still_remaining

                if net_progress(before_count, len(remaining)) >= 0:
                    break

            if auto_fixes:
                self.fix_branch = branch_name

        finally:
            sandbox.discard()

        return auto_fixes, remaining


# =============================================================================
# Helpers
# =============================================================================


def _format_issues_for_llm(issues: list[CodeFinding]) -> str:
    """Format a batch of issues as a numbered list for LLM consumption.

    Delegates to the shared ``format_findings_for_llm`` utility.
    """
    return format_findings_for_llm(issues)


def _build_verify_command(config: QualityGateConfig) -> str:
    """Build a shell command string that verifies code quality."""
    parts = ["ruff check .", "ruff format --check ."]
    if config.mypy_target and config.mypy_target != ".":
        parts.append(f"mypy {config.mypy_target}")
    test_cmd = "pytest -x -q"
    if config.test_paths:
        test_cmd += " " + " ".join(config.test_paths)
    parts.append(test_cmd)
    return " && ".join(parts)


def _split_diff_per_file(diff_text: str) -> dict[str, str]:
    """Split a unified diff into per-file chunks."""
    import re

    files: dict[str, str] = {}
    current_file: str | None = None
    current_lines: list[str] = []

    for line in diff_text.splitlines(keepends=True):
        if line.startswith("diff --git"):
            if current_file is not None:
                files[current_file] = "".join(current_lines)
            m = re.match(r"diff --git a/(.+?) b/(.+)", line)
            current_file = m.group(2) if m else "unknown"
            current_lines = [line]
        else:
            current_lines.append(line)

    if current_file is not None:
        files[current_file] = "".join(current_lines)

    return files


def _condense_diff(
    per_file: dict[str, str],
    max_total_chars: int = 30000,
    max_per_file_chars: int = 3000,
) -> str:
    """Condense per-file diffs into a single string within size limits."""
    parts: list[str] = []
    total = 0

    for file_path, file_diff in per_file.items():
        if total >= max_total_chars:
            parts.append(f"\n... ({len(per_file) - len(parts)} more files not shown)\n")
            break

        if len(file_diff) > max_per_file_chars:
            truncated = file_diff[:max_per_file_chars]
            truncated += f"\n... (truncated, {len(file_diff)} chars total for {file_path})\n"
            parts.append(truncated)
            total += len(truncated)
        else:
            parts.append(file_diff)
            total += len(file_diff)

    return "".join(parts)


def _discover_base_ref(workspace_path: str) -> str:
    """Auto-discover the diff base from the most recently opened PR."""
    import shutil

    gh_path = shutil.which("gh")
    if gh_path is None:
        return "main"

    result = subprocess.run(
        [gh_path, "pr", "list", "--state", "open", "--limit", "1", "--json", "headRefName"],
        check=False,
        cwd=workspace_path,
        capture_output=True,
        text=True,
        timeout=15,
    )

    if result.returncode == 0 and result.stdout.strip():
        import json

        try:
            prs = json.loads(result.stdout)
            if prs and prs[0].get("headRefName"):
                head = prs[0]["headRefName"]
                check = subprocess.run(
                    ["git", "rev-parse", "--verify", head],
                    check=False,
                    cwd=workspace_path,
                    capture_output=True,
                    text=True,
                )
                if check.returncode == 0:
                    return head
                check = subprocess.run(
                    ["git", "rev-parse", "--verify", f"origin/{head}"],
                    check=False,
                    cwd=workspace_path,
                    capture_output=True,
                    text=True,
                )
                if check.returncode == 0:
                    return f"origin/{head}"
        except (json.JSONDecodeError, KeyError, IndexError):
            pass

    return "main"


def _get_tool_result(stage_result: object, default_tool: str) -> ToolRunResult:
    """Extract ToolRunResult from a completed stage, with safe fallback."""
    result = getattr(stage_result, "result", None)
    if isinstance(result, ToolRunResult):
        return result
    return ToolRunResult(tool=default_tool, passed=True, skipped=True, skip_reason="stage produced no result")


def _get_changed_files(workspace_path: str, base_ref: str) -> list[str]:
    for ref in [base_ref, f"origin/{base_ref}"]:
        result = subprocess.run(
            ["git", "diff", "--name-only", ref],
            check=False,
            cwd=workspace_path,
            capture_output=True,
            text=True,
        )
        if result.returncode == 0 and result.stdout.strip():
            return [f for f in result.stdout.strip().splitlines() if f]
    return []


def _get_diff(workspace_path: str, base_ref: str, files: list[str] | None = None) -> str:
    for ref in [base_ref, f"origin/{base_ref}"]:
        cmd = ["git", "diff", ref]
        if files:
            cmd.append("--")
            cmd.extend(files)
        result = subprocess.run(cmd, check=False, cwd=workspace_path, capture_output=True, text=True)
        if result.returncode == 0:
            return result.stdout
    return ""


def _filter_paths(
    files: list[str],
    include: list[str],
    exclude: list[str],
) -> list[str]:
    """Filter file list by include/exclude prefixes."""
    result = files
    if include:
        result = [f for f in result if any(f.startswith(p) for p in include)]
    if exclude:
        result = [f for f in result if not any(f.startswith(p) for p in exclude)]
    return result


def _get_commit_log(workspace_path: str, base_ref: str) -> str:
    for ref in [base_ref, f"origin/{base_ref}"]:
        result = subprocess.run(
            ["git", "log", "--oneline", f"{ref}..HEAD"],
            check=False,
            cwd=workspace_path,
            capture_output=True,
            text=True,
        )
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip()
    return ""


def _template_description(
    changed_files: list[str],
    commit_log: str,
    verdict: QualityGateVerdict,
    tool_results: list[ToolRunResult],
    unresolved: list[CodeFinding],
    auto_fixes: list[FixRecord],
) -> tuple[str, str]:
    """Generate a template-based PR description (no LLM)."""
    first_commit = (
        commit_log.strip().splitlines()[0] if commit_log.strip() else f"feat: update {len(changed_files)} file(s)"
    )

    sections = ["## Summary\n", f"This PR modifies {len(changed_files)} file(s).\n"]

    if changed_files:
        sections.append("### Files changed\n")
        for f in changed_files[:20]:
            sections.append(f"- `{f}`")
        if len(changed_files) > 20:
            sections.append(f"- ... and {len(changed_files) - 20} more")
        sections.append("")

    if auto_fixes:
        sections.append("### Automated fixes applied\n")
        for fix in auto_fixes:
            verified = "verified" if fix.verified else "unverified"
            f = fix.finding or fix.issue
            desc = f.description if f else "unknown"
            sections.append(f"- {desc} ({verified})")
        sections.append("")

    sections.append("### Quality gate results\n")
    for tr in tool_results:
        status = "SKIP" if tr.skipped else ("PASS" if tr.passed else "FAIL")
        detail = tr.skip_reason if tr.skipped else f"{len(tr.findings)} issue(s)"
        sections.append(f"- {tr.tool}: {status} ({detail})")
    sections.append(f"\n**Verdict:** `{verdict}`\n")
    sections.append("## Test plan\n- [ ] CI passes\n- [ ] Review automated changes")

    return first_commit, "\n".join(sections)
