"""Owner-path context-driven config inference and resolution.

This module remains available as ``shepherd.autoconfig`` for workflow migration
and extras that still use class-form task metadata. It is not exported from the
top-level callable-spine facade.

Provides the high-level autoconfig API:

- ``infer_from_context`` — LLM-driven inference of config field values
- ``resolve_config`` — batteries-included resolution chain (cached → inferred → defaults)
- ``discover_config`` / ``persist_config`` — YAML config persistence
- ``WORKSPACE_ANALYSIS_GUIDANCE`` — shared exploration guidance for workspace analysis

Usage::

    from shepherd.autoconfig import resolve_config
    from shepherd_runtime.scope import Scope

    with Scope():
        config = resolve_config(PRReviewConfig)
"""

from __future__ import annotations

import contextlib
import logging
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, TypeVar

import yaml
from pydantic import BaseModel, Field, ValidationError
from shepherd_core.autoconfig import build_inference_model, extract_infer_fields
from shepherd_runtime.task.decorator import _apply_task_decorator
from shepherd_runtime.task.markers import Output

logger = logging.getLogger(__name__)

T = TypeVar("T", bound=BaseModel)

# =============================================================================
# Shared workspace analysis guidance
# =============================================================================

WORKSPACE_ANALYSIS_GUIDANCE = """\
You are analyzing a software repository to infer configuration values.

## Exploration strategy

Read files in this order of priority — stop as soon as you have enough signal:

1. **CI configuration**: `.github/workflows/*.yml`, `.gitlab-ci.yml`, `Jenkinsfile`,
   `Makefile` — extract build/test/lint commands.
2. **Project metadata**: `pyproject.toml`, `package.json`, `Cargo.toml`, `go.mod` —
   identify language, framework, dependencies.
3. **Linter/formatter config**: `ruff.toml`, `.eslintrc.*`, `.prettierrc`, `rustfmt.toml`
   — understand code style expectations and excluded paths.
4. **Contribution docs**: `CONTRIBUTING.md`, `.github/PULL_REQUEST_TEMPLATE.md`,
   `docs/contributing.*` — extract review norms and quality expectations.
5. **Git ignore patterns**: `.gitignore`, `.dockerignore` — identify generated or
   vendored paths to exclude from review.
6. **Repository structure**: Use Glob to understand the directory layout. Read a few
   representative source files if the purpose of a directory is unclear.

## Principles

- **Prefer existing CI commands** over inventing new ones. If the repo runs
  `pytest tests/ -x` in CI, use that exact command — don't guess.
- **Infer from what exists.** If there's no CONTRIBUTING.md, don't fabricate
  guidelines — leave the field empty and let defaults apply.
- **Skip uncertain fields.** If you can't determine a value with reasonable
  confidence, leave it as null/empty. A wrong value is worse than a default.
- **Infrastructure fields are not your concern.** Fields like repository name,
  authentication tokens, and clone URLs are populated at runtime by the
  pipeline. Leave them null.
"""


# =============================================================================
# Exceptions
# =============================================================================


class NoContextError(RuntimeError):
    """Raised when infer_from_context cannot find an analysis context."""


# =============================================================================
# Config persistence helpers
# =============================================================================


def config_name(config_cls: type[BaseModel]) -> str:
    """Derive a file stem from the config class name.

    CamelCase -> snake_case, stripping the 'Config' suffix.
    PRReviewConfig -> pr_review, PrePRChecksConfig -> pre_pr_checks.
    """
    name = config_cls.__name__
    if name.endswith("Config") and name != "Config":
        name = name[: -len("Config")]
    s1 = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1_\2", name)
    s2 = re.sub(r"([a-z\d])([A-Z])", r"\1_\2", s1)
    return s2.lower()


def discover_config(
    config_cls: type[T],
    config_dir: str = ".shepherd",
) -> T | None:
    """Search for a persisted config YAML and load it.

    Searches cwd and repo root for ``{config_dir}/{name}.yaml/.yml``.
    Returns ``None`` if not found or on parse/validation error.
    """
    name = config_name(config_cls)
    candidates: list[Path] = [
        Path(config_dir) / f"{name}.yaml",
        Path(config_dir) / f"{name}.yml",
    ]

    try:
        result = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            check=True,
        )
        repo_root = Path(result.stdout.strip())
        candidates.extend(
            [
                repo_root / config_dir / f"{name}.yaml",
                repo_root / config_dir / f"{name}.yml",
            ]
        )
    except (subprocess.CalledProcessError, FileNotFoundError):
        pass

    seen: set[Path] = set()
    unique_candidates: list[Path] = []
    for c in candidates:
        resolved = c.resolve()
        if resolved not in seen:
            seen.add(resolved)
            unique_candidates.append(c)

    for path in unique_candidates:
        if path.exists():
            try:
                with path.open() as f:
                    data: dict[str, Any] = yaml.safe_load(f) or {}
                return config_cls.model_validate(data)
            except (yaml.YAMLError, ValidationError) as e:
                logger.warning(
                    "Failed to load config from %s (%s): %s",
                    path,
                    type(e).__name__,
                    e,
                )
                return None

    return None


def persist_config(
    config: BaseModel,
    config_dir: str = ".shepherd",
) -> Path:
    """Write the config to YAML with a generation timestamp.

    Only persists ``Infer``-annotated fields (the values that were inferred).
    Non-inferable fields (runtime secrets, internal flags) are excluded from
    the YAML so users aren't confused by fields they shouldn't edit.

    Returns the path written to.
    """
    name = config_name(type(config))
    dir_path = Path(config_dir)
    dir_path.mkdir(parents=True, exist_ok=True)

    path = dir_path / f"{name}.yaml"
    full_data = config.model_dump(mode="json")

    # Only persist inferable fields — exclude runtime/infrastructure fields
    infer_fields = extract_infer_fields(type(config))
    if infer_fields:
        data = {k: full_data[k] for k in infer_fields if k in full_data}
    else:
        data = full_data

    timestamp = datetime.now(tz=timezone.utc).isoformat(timespec="seconds")
    header = (
        f"# Auto-generated by autoconfig on {timestamp}\n"
        f"# Edit freely — this file takes precedence over inference.\n"
        f"# Re-infer with: resolve_config({type(config).__name__}, force=True)\n\n"
    )

    with path.open("w") as f:
        f.write(header)
        yaml.safe_dump(data, f, default_flow_style=False, sort_keys=False)

    return path


# =============================================================================
# Dynamic task construction
# =============================================================================


def _build_inference_task(
    target_cls: type[BaseModel],
    guidance: str = "",
) -> type:
    """Build a @task class at runtime for config inference.

    Creates a task with a single ``Output(FilteredModel)`` field wrapping
    the filtered Pydantic model. This matches the ``ConfigurePRReview``
    pattern and avoids the framework's None-stripping issue.
    """
    infer_fields = extract_infer_fields(target_cls)
    if not infer_fields:
        msg = f"No Infer-annotated fields found on {target_cls.__name__}"
        raise ValueError(msg)

    filtered_model = build_inference_model(target_cls)

    # Build docstring from field descriptions
    doc_lines = [f"Infer configuration for {target_cls.__name__}."]
    for name, info in infer_fields.items():
        doc_lines.append(f"\n{name}: {info['description']}")

    namespace: dict[str, Any] = {
        "__annotations__": {"config": Output(filtered_model)},
        "config": Field(description=f"Inferred configuration for {target_cls.__name__}"),
        "__doc__": "\n".join(doc_lines),
    }

    task_name = f"InferConfig_{target_cls.__name__}"
    task_cls = type(task_name, (BaseModel,), namespace)

    full_guidance = guidance or WORKSPACE_ANALYSIS_GUIDANCE
    class_guidance = getattr(target_cls, "__infer_guidance__", None)
    if class_guidance:
        full_guidance = f"{full_guidance}\n\n{class_guidance}"

    return _apply_task_decorator(task_cls, guidance=full_guidance)


# =============================================================================
# Core inference primitive
# =============================================================================


def infer_from_context(
    target: type[BaseModel],
    *,
    context: Any | None = None,
    hints: str = "",
) -> dict[str, Any]:
    """Infer values for Infer-annotated fields from an analysis context.

    Builds a dynamic @task class, executes it in a forked scope, and
    returns the inferred field values as a dict.

    Args:
        target: A Pydantic BaseModel with Infer-annotated fields.
        context: Analysis context (e.g., WorkspaceRef). If None, uses the
            WorkspaceRef already bound in the current scope.
        hints: Optional natural-language hints appended to the guidance.

    Returns:
        Dict mapping field names to inferred values. Only contains fields
        that are Infer-annotated on the target class.

    Raises:
        NoContextError: If no context is available.
        ValueError: If the target has no Infer-annotated fields.
    """
    from shepherd_runtime.session import current_scope

    scope = current_scope()
    if scope is None:
        raise NoContextError("No scope available for inference. Run inside shepherd_runtime.scope.Scope.")

    # Verify a context is available (either explicit or in scope)
    if context is None:
        has_bindings = False
        with contextlib.suppress(AttributeError, TypeError):
            has_bindings = bool(list(scope.all_bindings()))
        if not has_bindings:
            raise NoContextError(
                "No context bound in scope. Bind a WorkspaceRef or pass an explicit context parameter."
            )

    # Build the guidance
    guidance = WORKSPACE_ANALYSIS_GUIDANCE
    if hints:
        guidance = f"{guidance}\n\n## Additional context\n\n{hints}"

    # Build and execute the dynamic task
    task_cls = _build_inference_task(target, guidance=guidance)

    child_scope = scope.fork()
    if context is not None:
        binding_name = getattr(context, "__binding_name__", None) or "context"
        child_scope.bind(binding_name, context)

    try:
        with child_scope:
            result = task_cls()

        scope.merge(child_scope)
    except Exception:
        if not child_scope.is_discarded:
            child_scope.discard()
        raise

    # Extract the config output and return as dict
    config_output = result.config
    if config_output is None:
        msg = f"Inference produced no output for {target.__name__}"
        raise RuntimeError(msg)

    return config_output.model_dump()


# =============================================================================
# Batteries-included config resolution
# =============================================================================


def resolve_config(
    config_cls: type[T],
    partial: T | None = None,
    *,
    persist: bool = True,
    force: bool = False,
    config_dir: str = ".shepherd",
    context: Any | None = None,
    hints: str = "",
) -> T:
    """Resolve config through the full chain: cached -> inferred -> defaults.

    Resolution order:
        1. Cached YAML (unless ``force=True``)
        2. LLM inference via ``infer_from_context``
        3. Default values from the config class

    At each stage, fields explicitly set in ``partial`` (via
    ``model_fields_set``) take precedence over all other sources.

    Args:
        config_cls: The Pydantic config class.
        partial: Optional partial config with explicit overrides.
        persist: Persist inferred configs to .shepherd/ for future discovery.
        force: Skip cache lookup, always re-infer.
        config_dir: Directory for YAML config files.
        context: Analysis context for inference.
        hints: Natural-language hints for inference.

    Returns:
        A complete, validated config instance.
    """
    explicit_fields: set[str] = set()
    if partial is not None:
        explicit_fields = partial.model_fields_set

    # Step 1: Cached YAML
    if not force:
        cached = discover_config(config_cls, config_dir=config_dir)
        if cached is not None:
            return _merge_config(config_cls, cached, partial, explicit_fields)

    # Step 2: LLM inference
    try:
        inferred_dict = infer_from_context(config_cls, context=context, hints=hints)
        inferred = config_cls.model_validate(inferred_dict)

        if persist:
            persist_config(inferred, config_dir=config_dir)

        return _merge_config(config_cls, inferred, partial, explicit_fields)
    except NoContextError:
        logger.debug(
            "No context available for %s inference, using defaults",
            config_cls.__name__,
        )
    except Exception:  # noqa: BLE001
        logger.warning(
            "Config inference failed for %s, falling through to defaults",
            config_cls.__name__,
            exc_info=True,
        )

    # Step 3: Defaults
    defaults = config_cls()
    return _merge_config(config_cls, defaults, partial, explicit_fields)


def _merge_config(
    config_cls: type[T],
    base: T,
    partial: T | None,
    explicit_fields: set[str],
) -> T:
    """Merge a base config with partial overrides.

    Fields in ``explicit_fields`` (from ``partial.model_fields_set``) take
    precedence over the base config.
    """
    if partial is None or not explicit_fields:
        return base

    base_dict = base.model_dump()
    partial_dict = partial.model_dump()

    for field_name in explicit_fields:
        if field_name in partial_dict:
            base_dict[field_name] = partial_dict[field_name]

    return config_cls.model_validate(base_dict)


__all__ = [
    "WORKSPACE_ANALYSIS_GUIDANCE",
    "NoContextError",
    "config_name",
    "discover_config",
    "infer_from_context",
    "persist_config",
    "resolve_config",
]
