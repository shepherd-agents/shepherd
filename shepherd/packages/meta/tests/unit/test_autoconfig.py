"""Tests for shepherd.autoconfig: infer_from_context, resolve_config, and helpers."""

from __future__ import annotations

from pathlib import Path  # noqa: TC003 (runtime: used in pytest fixtures)
from typing import Annotated

import pytest
from pydantic import BaseModel, Field
from shepherd.autoconfig import (
    NoContextError,
    _build_inference_task,
    config_name,
    discover_config,
    infer_from_context,
    persist_config,
    resolve_config,
)
from shepherd_core import Infer
from shepherd_runtime.scope import Scope
from shepherd_tests import MockProvider

# =============================================================================
# Fixtures
# =============================================================================


class VerifyConfig(BaseModel):
    test_command: str = Field(description="Test command")
    build_command: str | None = Field(default=None, description="Build step")


class SampleConfig(BaseModel):
    guidelines: Annotated[str, Infer] = Field(
        default="",
        description="Repo-specific review standards. Synthesize from CONTRIBUTING.md.",
    )
    focus_areas: Annotated[list[str], Infer] = Field(
        default_factory=lambda: ["correctness", "security"],
        description="Review focus areas derived from repository structure.",
    )
    verify: Annotated[VerifyConfig | None, Infer] = Field(
        default=None,
        description="Build/test verification, or null to skip.",
    )
    max_comments: int = Field(default=5, ge=1)
    repo: str | None = None


MOCK_OUTPUT = {
    "config": {
        "guidelines": "Follow PEP 8. Write tests.",
        "focus_areas": ["correctness", "security"],
        "verify": None,
    },
}


def _make_provider() -> MockProvider:
    return MockProvider(name="test", structured_output=MOCK_OUTPUT)


# =============================================================================
# _build_inference_task
# =============================================================================


class TestBuildInferenceTask:
    def test_creates_task(self) -> None:
        task_cls = _build_inference_task(SampleConfig)
        assert hasattr(task_cls, "_task_meta")

    def test_synthetic_name(self) -> None:
        task_cls = _build_inference_task(SampleConfig)
        assert task_cls.__name__ == "InferConfig_SampleConfig"

    def test_single_output_field(self) -> None:
        task_cls = _build_inference_task(SampleConfig)
        assert "config" in task_cls._task_meta.outputs
        assert len(task_cls._task_meta.outputs) == 1

    def test_guidance_in_metadata(self) -> None:
        task_cls = _build_inference_task(SampleConfig, guidance="Custom")
        assert "Custom" in task_cls._task_meta.guidance

    def test_infer_guidance_appended(self) -> None:
        class WithGuidance(SampleConfig):
            __infer_guidance__ = "Aggregate per-package configs."

        task_cls = _build_inference_task(WithGuidance, guidance="Base")
        assert "Base" in task_cls._task_meta.guidance
        assert "Aggregate" in task_cls._task_meta.guidance

    def test_raises_on_no_infer_fields(self) -> None:
        class Empty(BaseModel):
            x: int = 1

        with pytest.raises(ValueError, match="No Infer-annotated"):
            _build_inference_task(Empty)


# =============================================================================
# infer_from_context
# =============================================================================


class TestInferFromContext:
    def test_returns_inferred_dict(self) -> None:
        provider = _make_provider()
        with Scope(root=True) as scope:
            scope.register_provider("default", provider, default=True)
            # Bind a dummy context so the "has bindings" check passes
            scope.bind("workspace", _DummyContext())
            result = infer_from_context(SampleConfig)

        assert result["guidelines"] == "Follow PEP 8. Write tests."
        assert result["focus_areas"] == ["correctness", "security"]
        assert result["verify"] is None

    def test_raises_no_context_without_scope(self) -> None:
        with pytest.raises(NoContextError):
            infer_from_context(SampleConfig)

    def test_raises_no_context_empty_scope(self) -> None:
        with Scope(root=True) as scope:
            scope.register_provider("default", _make_provider(), default=True)
            with pytest.raises(NoContextError, match="No context bound"):
                infer_from_context(SampleConfig)

    def test_result_constructs_original_config(self) -> None:
        provider = _make_provider()
        with Scope(root=True) as scope:
            scope.register_provider("default", provider, default=True)
            scope.bind("workspace", _DummyContext())
            result = infer_from_context(SampleConfig)

        config = SampleConfig(**result)
        assert config.guidelines == "Follow PEP 8. Write tests."
        assert config.max_comments == 5  # default preserved
        assert config.repo is None


# =============================================================================
# config_name
# =============================================================================


class TestConfigName:
    def test_strips_config_suffix(self) -> None:
        class PRReviewConfig(BaseModel):
            pass

        assert config_name(PRReviewConfig) == "pr_review"

    def test_bare_config(self) -> None:
        class Config(BaseModel):
            pass

        assert config_name(Config) == "config"

    def test_no_suffix(self) -> None:
        class SimpleModel(BaseModel):
            pass

        assert config_name(SimpleModel) == "simple_model"

    def test_consecutive_caps(self) -> None:
        class APIConfig(BaseModel):
            pass

        assert config_name(APIConfig) == "api"


# =============================================================================
# discover_config / persist_config round-trip
# =============================================================================


class TestConfigPersistence:
    def test_round_trip(self, tmp_path: Path) -> None:
        config = SampleConfig(
            guidelines="Be strict",
            focus_areas=["correctness"],
            verify=None,
        )
        persist_config(config, config_dir=str(tmp_path))

        loaded = discover_config(SampleConfig, config_dir=str(tmp_path))
        assert loaded is not None
        assert loaded.guidelines == "Be strict"
        assert loaded.focus_areas == ["correctness"]
        assert loaded.verify is None

    def test_returns_none_missing(self, tmp_path: Path) -> None:
        assert discover_config(SampleConfig, config_dir=str(tmp_path)) is None

    def test_returns_none_malformed(self, tmp_path: Path) -> None:
        name = config_name(SampleConfig)
        path = tmp_path / f"{name}.yaml"
        path.write_text("{invalid: [yaml: ")
        assert discover_config(SampleConfig, config_dir=str(tmp_path)) is None


# =============================================================================
# resolve_config
# =============================================================================


class TestResolveConfig:
    def test_returns_cached(self, tmp_path: Path) -> None:
        config = SampleConfig(guidelines="cached value")
        persist_config(config, config_dir=str(tmp_path))

        result = resolve_config(SampleConfig, config_dir=str(tmp_path))
        assert result.guidelines == "cached value"

    def test_falls_through_to_defaults(self) -> None:
        # No scope → inference fails → defaults
        result = resolve_config(SampleConfig, persist=False)
        assert result.guidelines == ""
        assert result.focus_areas == ["correctness", "security"]
        assert result.max_comments == 5

    def test_partial_overrides_win(self, tmp_path: Path) -> None:
        cached = SampleConfig(guidelines="from cache", focus_areas=["cached"])
        persist_config(cached, config_dir=str(tmp_path))

        partial = SampleConfig(guidelines="explicit override")
        result = resolve_config(SampleConfig, partial, config_dir=str(tmp_path))
        assert result.guidelines == "explicit override"
        assert result.focus_areas == ["cached"]  # from cache, not overridden

    def test_force_skips_cache(self, tmp_path: Path) -> None:
        cached = SampleConfig(guidelines="cached")
        persist_config(cached, config_dir=str(tmp_path))

        # force=True, no scope → inference fails → defaults
        result = resolve_config(SampleConfig, force=True, persist=False, config_dir=str(tmp_path))
        assert result.guidelines == ""  # defaults, not cached

    def test_inferred_is_persisted(self, tmp_path: Path) -> None:
        provider = _make_provider()
        with Scope(root=True) as scope:
            scope.register_provider("default", provider, default=True)
            scope.bind("workspace", _DummyContext())
            result = resolve_config(SampleConfig, config_dir=str(tmp_path))

        assert result.guidelines == "Follow PEP 8. Write tests."

        # Should have been persisted
        loaded = discover_config(SampleConfig, config_dir=str(tmp_path))
        assert loaded is not None
        assert loaded.guidelines == "Follow PEP 8. Write tests."


# =============================================================================
# Dummy context for scope binding
# =============================================================================


class _DummyContext:
    """Minimal context that satisfies scope.bind() requirements."""

    __binding_name__ = "workspace"
    context_id = "dummy:workspace"

    @property
    def reversibility(self):  # type: ignore[override]
        from shepherd_core.types import ReversibilityLevel

        return ReversibilityLevel.AUTO

    def configure(self, capabilities=None):  # type: ignore[override]
        from shepherd_core.types import ProviderBinding

        return ProviderBinding()

    def prepare(self):  # type: ignore[override]
        return self

    def extract_effects(self, sandbox=None, result=None):  # type: ignore[override]
        return []

    def apply_effect(self, effect=None):  # type: ignore[override]
        return self

    def cleanup(self, error=None):  # type: ignore[override]
        pass

    def to_state(self):  # type: ignore[override]
        return {}

    @classmethod
    def from_state(cls, state=None, sandbox_path=None):  # type: ignore[override]
        return cls()

    def transfer_bundle(self, scope=None):  # type: ignore[override]
        return None
