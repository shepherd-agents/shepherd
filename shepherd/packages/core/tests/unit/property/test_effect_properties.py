"""Property-based tests for Effect serialization invariants.

Tests core invariants of the Effect system:
1. Roundtrip preservation: model_dump/model_validate preserves all fields
2. Type dispatch: effect_from_dict uses effect_type correctly
3. Immutability: Frozen models cannot be modified
4. Registry completeness: All effect types are registered
"""

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from pydantic import ValidationError
from shepherd_core.effects import (
    EFFECT_TYPES,
    KERNEL_EFFECT_REGISTRY,
    ContextConfigured,
    Effect,
    FileCreate,
    FileRead,
    TaskCompleted,
    TaskFailed,
    TaskStarted,
    ToolCallCompleted,
    ToolCallStarted,
    effect_from_dict,
)

# =============================================================================
# Hypothesis Strategies
# =============================================================================


# Simple alphanumeric text for identifiers
identifier_text = st.text(
    min_size=1,
    max_size=30,
    alphabet=st.characters(whitelist_categories=("L", "N", "Pd"), whitelist_characters="_-"),
)

# Optional identifier
optional_identifier = st.none() | identifier_text

# Simple dict for inputs/outputs/params
simple_dict = st.dictionaries(
    keys=st.text(min_size=1, max_size=15, alphabet=st.characters(whitelist_categories=("L", "N"))),
    values=st.text(max_size=100) | st.integers() | st.booleans(),
    max_size=5,
)


@st.composite
def task_started_strategy(draw: st.DrawFn) -> TaskStarted:
    """Generate TaskStarted effects."""
    return TaskStarted(
        task_name=draw(identifier_text),
        provider_id=draw(optional_identifier),
        context_id=draw(optional_identifier),
        binding_name=draw(optional_identifier),
        inputs=draw(simple_dict),
    )


@st.composite
def task_completed_strategy(draw: st.DrawFn) -> TaskCompleted:
    """Generate TaskCompleted effects."""
    return TaskCompleted(
        task_name=draw(identifier_text),
        provider_id=draw(optional_identifier),
        context_id=draw(optional_identifier),
        binding_name=draw(optional_identifier),
        outputs=draw(simple_dict),
        duration_ms=draw(st.floats(min_value=0, max_value=100000, allow_nan=False, allow_infinity=False)),
    )


@st.composite
def task_failed_strategy(draw: st.DrawFn) -> TaskFailed:
    """Generate TaskFailed effects."""
    return TaskFailed(
        task_name=draw(identifier_text),
        provider_id=draw(optional_identifier),
        context_id=draw(optional_identifier),
        binding_name=draw(optional_identifier),
        error=draw(st.text(max_size=200)),
        error_type=draw(st.text(min_size=1, max_size=50)),
        phase=draw(st.sampled_from(["configure", "prepare", "execute", "extract", "apply", "cleanup", ""])),
    )


@st.composite
def tool_call_started_strategy(draw: st.DrawFn) -> ToolCallStarted:
    """Generate ToolCallStarted effects."""
    return ToolCallStarted(
        task_name=draw(identifier_text),
        provider_id=draw(optional_identifier),
        context_id=draw(optional_identifier),
        binding_name=draw(optional_identifier),
        tool_call_id=draw(identifier_text),
        tool_name=draw(identifier_text),
        params=draw(simple_dict),
    )


@st.composite
def tool_call_completed_strategy(draw: st.DrawFn) -> ToolCallCompleted:
    """Generate ToolCallCompleted effects."""
    return ToolCallCompleted(
        task_name=draw(identifier_text),
        provider_id=draw(optional_identifier),
        context_id=draw(optional_identifier),
        binding_name=draw(optional_identifier),
        tool_call_id=draw(identifier_text),
        tool_name=draw(identifier_text),
        success=draw(st.booleans()),
        output=draw(st.text(max_size=100)),  # Required string, not optional
    )


@st.composite
def file_create_strategy(draw: st.DrawFn) -> FileCreate:
    """Generate FileCreate effects."""
    return FileCreate(
        task_name=draw(identifier_text),
        provider_id=draw(optional_identifier),
        context_id=draw(optional_identifier),
        binding_name=draw(optional_identifier),
        path=draw(st.text(min_size=1, max_size=100)),
        content=draw(st.text(max_size=500)),
        caused_by=draw(optional_identifier),
    )


@st.composite
def file_read_strategy(draw: st.DrawFn) -> FileRead:
    """Generate FileRead effects."""
    return FileRead(
        task_name=draw(identifier_text),
        provider_id=draw(optional_identifier),
        context_id=draw(optional_identifier),
        binding_name=draw(optional_identifier),
        path=draw(st.text(min_size=1, max_size=100)),
    )


@st.composite
def context_configured_strategy(draw: st.DrawFn) -> ContextConfigured:
    """Generate ContextConfigured effects."""
    # Note: ContextConfigured overrides binding_name to be str (not optional)
    return ContextConfigured(
        task_name=draw(identifier_text),
        provider_id=draw(optional_identifier),
        context_id=draw(optional_identifier),
        binding_name=draw(identifier_text),  # Required in ContextConfigured
    )


# Combined strategy for any effect
any_effect_strategy = st.one_of(
    task_started_strategy(),
    task_completed_strategy(),
    task_failed_strategy(),
    tool_call_started_strategy(),
    tool_call_completed_strategy(),
    file_create_strategy(),
    file_read_strategy(),
    context_configured_strategy(),
)


# =============================================================================
# Property Tests: Roundtrip Preservation
# =============================================================================


class TestEffectRoundtrip:
    """Tests for Effect serialization roundtrip invariant."""

    @given(effect=task_started_strategy())
    @settings(max_examples=50)
    def test_task_started_roundtrip(self, effect: TaskStarted) -> None:
        """TaskStarted should roundtrip through model_dump/model_validate."""
        data = effect.model_dump()
        restored = TaskStarted.model_validate(data)

        assert restored.task_name == effect.task_name
        assert restored.provider_id == effect.provider_id
        assert restored.context_id == effect.context_id
        assert restored.inputs == effect.inputs
        assert restored.effect_type == effect.effect_type

    @given(effect=task_completed_strategy())
    @settings(max_examples=50)
    def test_task_completed_roundtrip(self, effect: TaskCompleted) -> None:
        """TaskCompleted should roundtrip through model_dump/model_validate."""
        data = effect.model_dump()
        restored = TaskCompleted.model_validate(data)

        assert restored.task_name == effect.task_name
        assert restored.outputs == effect.outputs
        assert restored.duration_ms == effect.duration_ms

    @given(effect=tool_call_started_strategy())
    @settings(max_examples=50)
    def test_tool_call_started_roundtrip(self, effect: ToolCallStarted) -> None:
        """ToolCallStarted should roundtrip through model_dump/model_validate."""
        data = effect.model_dump()
        restored = ToolCallStarted.model_validate(data)

        assert restored.tool_call_id == effect.tool_call_id
        assert restored.tool_name == effect.tool_name
        assert restored.params == effect.params

    @given(effect=file_create_strategy())
    @settings(max_examples=50)
    def test_file_create_roundtrip(self, effect: FileCreate) -> None:
        """FileCreate should roundtrip through model_dump/model_validate."""
        data = effect.model_dump()
        restored = FileCreate.model_validate(data)

        assert restored.path == effect.path
        assert restored.content == effect.content
        assert restored.caused_by == effect.caused_by

    @given(effect=any_effect_strategy)
    @settings(max_examples=100)
    def test_any_effect_roundtrip_preserves_type(self, effect: Effect) -> None:
        """Any effect should preserve its type through roundtrip."""
        data = effect.model_dump()
        restored = type(effect).model_validate(data)

        assert type(restored) == type(effect)
        assert restored.effect_type == effect.effect_type


# =============================================================================
# Property Tests: Type Dispatch via effect_from_dict
# =============================================================================


class TestEffectTypeDispatch:
    """Tests for effect_from_dict type dispatch."""

    @given(effect=task_started_strategy())
    @settings(max_examples=50)
    def test_dispatch_task_started(self, effect: TaskStarted) -> None:
        """effect_from_dict should restore TaskStarted from dict."""
        data = effect.model_dump()
        restored = effect_from_dict(data)

        assert isinstance(restored, TaskStarted)
        assert restored.task_name == effect.task_name

    @given(effect=tool_call_completed_strategy())
    @settings(max_examples=50)
    def test_dispatch_tool_call_completed(self, effect: ToolCallCompleted) -> None:
        """effect_from_dict should restore ToolCallCompleted from dict."""
        data = effect.model_dump()
        restored = effect_from_dict(data)

        assert isinstance(restored, ToolCallCompleted)
        assert restored.tool_call_id == effect.tool_call_id

    def test_explicit_registry_restores_custom_effect(self) -> None:
        """Explicit registries should control non-kernel effect decode."""
        from typing import Literal

        class CustomRegistryEffect(Effect):
            effect_type: Literal["custom_registry_effect"] = "custom_registry_effect"
            payload: str = ""

        registry = KERNEL_EFFECT_REGISTRY.extend({"custom_registry_effect": CustomRegistryEffect})
        restored = effect_from_dict(
            {"effect_type": "custom_registry_effect", "payload": "ok"},
            registry=registry,
        )

        assert isinstance(restored, CustomRegistryEffect)
        assert restored.payload == "ok"

    def test_default_decode_ignores_ambient_effect_types_mutation(self) -> None:
        """Default decode should stay pinned to the kernel registry snapshot."""
        from typing import Literal

        class AmbientOnlyEffect(Effect):
            effect_type: Literal["ambient_only_effect"] = "ambient_only_effect"
            payload: str = ""

        EFFECT_TYPES["ambient_only_effect"] = AmbientOnlyEffect
        try:
            restored = effect_from_dict({"effect_type": "ambient_only_effect", "payload": "ignored"})
        finally:
            del EFFECT_TYPES["ambient_only_effect"]

        assert type(restored) is Effect
        assert restored.effect_type == "ambient_only_effect"

    @given(effect=any_effect_strategy)
    @settings(max_examples=100)
    def test_dispatch_preserves_concrete_type(self, effect: Effect) -> None:
        """effect_from_dict should restore the concrete effect type."""
        data = effect.model_dump()
        restored = effect_from_dict(data)

        assert type(restored) == type(effect)

    def test_dispatch_unknown_type_returns_base_effect(self) -> None:
        """effect_from_dict with unknown type should return base Effect."""
        data = {
            "effect_type": "unknown_effect_type_xyz",
            "task_name": "test",
        }
        restored = effect_from_dict(data)

        # Should return base Effect, not raise
        assert isinstance(restored, Effect)
        assert restored.effect_type == "unknown_effect_type_xyz"

    def test_dispatch_missing_type_returns_base_effect(self) -> None:
        """effect_from_dict without effect_type should use 'base' default."""
        data = {"task_name": "test"}
        restored = effect_from_dict(data)

        assert isinstance(restored, Effect)


# =============================================================================
# Property Tests: Immutability
# =============================================================================


class TestEffectImmutability:
    """Tests for Effect immutability (frozen models)."""

    @given(effect=task_started_strategy())
    @settings(max_examples=30)
    def test_task_started_is_frozen(self, effect: TaskStarted) -> None:
        """TaskStarted should be frozen (immutable)."""
        with pytest.raises(ValidationError):
            effect.task_name = "modified"  # type: ignore

    @given(effect=tool_call_started_strategy())
    @settings(max_examples=30)
    def test_tool_call_started_is_frozen(self, effect: ToolCallStarted) -> None:
        """ToolCallStarted should be frozen (immutable)."""
        with pytest.raises(ValidationError):
            effect.tool_name = "modified"  # type: ignore

    @given(effect=file_create_strategy())
    @settings(max_examples=30)
    def test_file_create_is_frozen(self, effect: FileCreate) -> None:
        """FileCreate should be frozen (immutable)."""
        with pytest.raises(ValidationError):
            effect.path = "modified"  # type: ignore


# =============================================================================
# Property Tests: Registry Completeness
# =============================================================================


class TestEffectRegistry:
    """Tests for EFFECT_TYPES registry completeness."""

    def test_all_tested_types_in_registry(self) -> None:
        """All effect types we test should be in the registry."""
        tested_types = [
            TaskStarted,
            TaskCompleted,
            TaskFailed,
            ToolCallStarted,
            ToolCallCompleted,
            FileCreate,
            FileRead,
            ContextConfigured,
        ]

        for effect_cls in tested_types:
            # Get the effect_type from the class
            effect_type_value = effect_cls.model_fields["effect_type"].default
            assert effect_type_value in EFFECT_TYPES, f"{effect_cls.__name__} not in registry"
            assert EFFECT_TYPES[effect_type_value] == effect_cls

    def test_registry_types_have_effect_type_field(self) -> None:
        """All registered effect types should have effect_type field with default."""
        for effect_type_key, effect_cls in EFFECT_TYPES.items():
            assert "effect_type" in effect_cls.model_fields
            default = effect_cls.model_fields["effect_type"].default
            assert default == effect_type_key, f"{effect_cls.__name__}: {default} != {effect_type_key}"

    @given(effect=any_effect_strategy)
    @settings(max_examples=50)
    def test_effect_type_field_matches_registry(self, effect: Effect) -> None:
        """Effect's effect_type field should match its registry key."""
        effect_type = effect.effect_type
        if effect_type in EFFECT_TYPES:
            assert EFFECT_TYPES[effect_type] == type(effect)


# =============================================================================
# Property Tests: Attribution Preservation
# =============================================================================


class TestEffectAttribution:
    """Tests for Effect attribution preservation through serialization."""

    @given(effect=any_effect_strategy)
    @settings(max_examples=50)
    def test_attribution_preserved_through_roundtrip(self, effect: Effect) -> None:
        """Attribution fields should be preserved through roundtrip."""
        data = effect.model_dump()
        restored = effect_from_dict(data)

        assert restored.task_name == effect.task_name
        assert restored.provider_id == effect.provider_id
        assert restored.context_id == effect.context_id
        assert restored.binding_name == effect.binding_name

    @given(effect=any_effect_strategy)
    @settings(max_examples=30)
    def test_timestamp_is_positive(self, effect: Effect) -> None:
        """Effect timestamp should be a positive float."""
        assert effect.timestamp > 0
        assert isinstance(effect.timestamp, float)
