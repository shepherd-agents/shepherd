"""ConfigureQualityGate — LLM-powered @task for auto-configuration.

Function-form (DECISIONS D5 / Tranche 7): the previous class-form
``@task class ConfigureQualityGate(BaseModel)`` is replaced with the
function-form ``@task async def configure_quality_gate(...) ->
QualityGateConfigResult`` shape per CONTRACTS A4.

Analyzes the codebase to infer quality gate configuration:
which tools to run, what targets to check, and what tests to execute.
Follows the same autoconfig pattern as configure_pr_review.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated

from shepherd.autoconfig import WORKSPACE_ANALYSIS_GUIDANCE
from shepherd_contexts.workspace.ref import WorkspaceRef
from shepherd_runtime.nucleus import deliver, task
from shepherd_runtime.scope import current_binding
from shepherd_runtime.task.markers import InputMarker

QUALITY_GATE_GUIDANCE = f"""\
{WORKSPACE_ANALYSIS_GUIDANCE}

## Quality Gate Configuration

You are inferring a `QualityGateConfig` for this repository's pre-push quality gate.
The quality gate runs lint, format, type-check, and test tools locally before push.

Focus on these fields:

### mypy_target (string)
Extract the exact mypy target from CI configuration:
- Look for `mypy <target>` in `.github/workflows/*.yml` or `Makefile`
- Common patterns: `mypy src/`, `mypy packages/pkg-name/src/pkg_name/`
- If CI doesn't run mypy, set to "" (empty = skip mypy)
- Do NOT default to "." — running mypy on the project root often fails
  because it tries to check non-Python directories.

### test_paths (list[str])
Extract test paths from CI configuration:
- Look for `pytest <paths>` in `.github/workflows/*.yml` or `Makefile`
- Include the exact paths and flags from CI, e.g., `["packages/", "-m", "not container and not e2e"]`
- If CI doesn't run tests, set to [] (empty = run pytest with no path args)

### test_ignore_paths (list[str])
Extract test paths that CI ignores:
- Look for `--ignore=<path>` flags in pytest commands
- Common: `["packages/pkg/tests/integration"]`

### base_ref (string)
The default branch to diff against:
- Usually "main" or "master"
- Check the repository's default branch

### mode (string)
Recommend a default mode based on project maturity:
- "fast" for projects with no CI or minimal tooling
- "standard" for projects with CI that runs lint + type-check + tests
- "full" for projects that want LLM-assisted code analysis

### Infrastructure fields — LEAVE AS DEFAULTS
The following are populated at runtime or by the caller:
- `workspace_path` — set by the entrypoint
- `model` — set by the caller
- `max_fix_iterations` — rarely needs changing
"""


@dataclass(frozen=True)
class QualityGateConfigResult:
    """Inferred quality-gate configuration for a repository."""

    mypy_target: str = "."
    test_paths: tuple[str, ...] = ()
    test_ignore_paths: tuple[str, ...] = ()
    base_ref: str = "main"
    mode: str = "standard"
    exclude_paths: tuple[str, ...] = ()
    include_paths: tuple[str, ...] = ()


@task(guidance=QUALITY_GATE_GUIDANCE)
async def configure_quality_gate(
    hints: Annotated[
        str, InputMarker(description="Optional user hints")
    ] = "",
) -> QualityGateConfigResult:
    """Analyze a codebase to infer quality gate configuration.

    The active workspace (looked up by type via
    ``current_binding(WorkspaceRef)``) provides read-only access to
    CI config, pyproject.toml, and project structure used to
    determine which tools to run and what targets to check.
    """
    workspace = current_binding(WorkspaceRef)
    return await deliver(
        QualityGateConfigResult,
        goal=(
            "Analyze the workspace and infer the QualityGateConfig "
            "fields (mypy_target, test_paths, test_ignore_paths, "
            "base_ref, mode, exclude_paths, include_paths) from CI "
            "config and project signals."
        ),
        evidence=[
            f"workspace={workspace.value}",
            f"hints={hints}",
        ],
    )


__all__ = ["QualityGateConfigResult", "configure_quality_gate"]
