"""Configuration models for the PR review workflow.

Progressive disclosure: zero-config works, YAML covers most needs,
full config surface available for advanced use.

Infer() fields are automatically filled by LLM workspace analysis when
using ``resolve_config(PRReviewConfig)``. Rich descriptions tell the LLM
how to derive each value — not just what the field means.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field
from shepherd_core import Infer  # noqa: TC002 (runtime: Pydantic resolves Infer annotations)


class VerifyConfig(BaseModel):
    """Configuration for the optional build/test verification step.

    When provided as ``PRReviewConfig.verify``, the pipeline runs
    build and test commands inside a Podman container before review.
    When ``None``, verification is skipped.
    """

    test_command: str = Field(description="Test execution command, e.g. 'pytest tests/ -x'")
    build_command: str | None = Field(default=None, description="Optional build step, e.g. 'make typecheck'")
    setup_commands: list[str] = Field(default_factory=list, description="Environment setup commands")
    container_image: str = Field(default="python:3.12", description="Container image for verification")


class PRReviewConfig(BaseModel):
    """Configuration for the PR review pipeline.

    All fields have sensible defaults. Most users will only set
    ``guidelines`` and optionally ``verify``.
    """

    # Review policy — Infer() fields are auto-filled by workspace analysis
    guidelines: Infer(str) = Field(
        default="",
        description=(
            "Repo-specific review standards. Synthesize from CONTRIBUTING.md, "
            "linter config, and README development sections. Keep to 2-4 concise "
            "sentences capturing the project's review philosophy. "
            "If no guidance exists in the repo, leave empty."
        ),
    )
    focus_areas: Infer(list[str]) = Field(
        default_factory=lambda: ["correctness", "security"],
        description=(
            "Review focus areas derived from repository structure and purpose. "
            "API-heavy projects: 'api-stability', 'backwards-compatibility'. "
            "Data pipelines: 'data-integrity', 'error-handling'. "
            "Security-sensitive: 'security', 'input-validation'. "
            "Always include 'correctness' unless the repo is trivially simple."
        ),
    )
    max_comments: int = Field(default=5, ge=1, description="Maximum findings to display prominently")
    file_patterns_to_skip: Infer(list[str]) = Field(
        default_factory=lambda: ["*.lock", "*.generated.*"],
        description=(
            "Glob patterns for files to exclude from review. Extend the defaults "
            "(*.lock, *.generated.*) with patterns from .gitignore entries for "
            "generated/vendored files and Ruff/ESLint exclude patterns. "
            "Common additions: 'vendor/**', 'dist/**', '*.min.js', '*.pb.go'."
        ),
    )

    # Verification
    verify: Infer(VerifyConfig | None) = Field(
        default=None,
        description=(
            "Build/test verification config, or null to skip. "
            "Populate ONLY if CI config reveals explicit test/build commands: "
            "test_command from the exact CI pytest/jest invocation, "
            "build_command from typecheck/compile steps if present, "
            "setup_commands from dependency install steps, "
            "container_image matching the CI image or 'python:3.12' as default. "
            "If no CI config exists, leave as null."
        ),
    )

    # Infrastructure — not Infer(), so mechanically excluded from inference
    repo: str | None = Field(default=None, description="Populated at runtime; leave null.")
    github_token: str | None = Field(default=None, description="Populated at runtime; leave null.")
    clone_url: str | None = Field(default=None, description="Populated at runtime; leave null.")

    @classmethod
    def from_yaml(cls, path: str | Path) -> PRReviewConfig:
        """Load configuration from a YAML file.

        Args:
            path: Path to YAML file (e.g. '.shepherd/review.yaml').

        Returns:
            Validated PRReviewConfig instance.

        Raises:
            FileNotFoundError: If the file doesn't exist.
            ValidationError: If the YAML content doesn't match the schema.
        """
        try:
            import yaml
        except ImportError:
            raise ImportError(
                "PyYAML is required for YAML config loading. Install it with: pip install pyyaml"
            ) from None

        path = Path(path)
        with path.open() as f:
            data: dict[str, Any] = yaml.safe_load(f) or {}

        return cls.model_validate(data)
