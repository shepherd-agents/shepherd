"""Property-based tests for Stream invariants.

Tests core invariants of the immutable Stream data structure:
1. Immutability: Operations return new instances
2. Sequence ordering: layer.sequence == index for all layers
3. Append-only: Effects never reorder
4. Truncation bounds: truncate_to(n) keeps exactly [0, n)
5. Serialization roundtrip: JSON serialize/deserialize preserves all data
6. Filter composition: Multiple filters use AND logic
"""

import json

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from shepherd_core.effects import (
    Effect,
    TaskCompleted,
    TaskStarted,
    ToolCallCompleted,
    ToolCallStarted,
)
from shepherd_core.scope.stream import Stream

pytestmark = pytest.mark.slow  # full-lifecycle suite: runs in the lifecycle-tests CI job

# =============================================================================
# Hypothesis Strategies
# =============================================================================


@st.composite
def effect_strategy(draw: st.DrawFn) -> Effect:
    """Generate arbitrary Effect instances."""
    effect_type = draw(
        st.sampled_from(
            [
                "task_started",
                "task_completed",
                "tool_call_started",
                "tool_call_completed",
            ]
        )
    )

    task_name = draw(st.text(min_size=1, max_size=20, alphabet=st.characters(whitelist_categories=("L", "N"))))
    provider_id = draw(
        st.none() | st.text(min_size=1, max_size=20, alphabet=st.characters(whitelist_categories=("L", "N")))
    )
    context_id = draw(
        st.none() | st.text(min_size=1, max_size=20, alphabet=st.characters(whitelist_categories=("L", "N")))
    )

    if effect_type == "task_started":
        return TaskStarted(
            task_name=task_name,
            provider_id=provider_id,
            context_id=context_id,
            inputs=draw(st.dictionaries(st.text(min_size=1, max_size=10), st.text(max_size=50), max_size=3)),
        )
    if effect_type == "task_completed":
        return TaskCompleted(
            task_name=task_name,
            provider_id=provider_id,
            context_id=context_id,
            outputs=draw(st.dictionaries(st.text(min_size=1, max_size=10), st.text(max_size=50), max_size=3)),
            duration_ms=draw(st.floats(min_value=0, max_value=10000, allow_nan=False)),
        )
    if effect_type == "tool_call_started":
        return ToolCallStarted(
            task_name=task_name,
            provider_id=provider_id,
            context_id=context_id,
            tool_call_id=draw(st.text(min_size=1, max_size=20)),
            tool_name=draw(st.text(min_size=1, max_size=20)),
            params=draw(st.dictionaries(st.text(min_size=1, max_size=10), st.text(max_size=50), max_size=3)),
        )
    # tool_call_completed
    return ToolCallCompleted(
        task_name=task_name,
        provider_id=provider_id,
        context_id=context_id,
        tool_call_id=draw(st.text(min_size=1, max_size=20)),
        tool_name=draw(st.text(min_size=1, max_size=20)),
        success=draw(st.booleans()),
        output=draw(st.text(max_size=100)),  # Required string, not optional
    )


@st.composite
def effect_list_strategy(draw: st.DrawFn, min_size: int = 0, max_size: int = 20) -> list[Effect]:
    """Generate a list of effects."""
    return draw(st.lists(effect_strategy(), min_size=min_size, max_size=max_size))


@st.composite
def stream_strategy(draw: st.DrawFn, min_size: int = 0, max_size: int = 20) -> Stream:
    """Generate a Stream with random effects."""
    effects = draw(effect_list_strategy(min_size=min_size, max_size=max_size))
    stream = Stream()
    for effect in effects:
        stream = stream.append(effect)
    return stream


# =============================================================================
# Property Tests: Immutability
# =============================================================================


class TestStreamImmutability:
    """Tests for Stream immutability invariant."""

    @given(stream=stream_strategy(), effect=effect_strategy())
    @settings(max_examples=100)
    def test_append_returns_new_instance(self, stream: Stream, effect: Effect) -> None:
        """append() should return a new Stream instance, never mutate original."""
        original_len = len(stream)
        original_id = id(stream)

        new_stream = stream.append(effect)

        # Original unchanged
        assert len(stream) == original_len
        assert id(stream) == original_id

        # New stream is different
        assert new_stream is not stream
        assert len(new_stream) == original_len + 1

    @given(stream=stream_strategy(min_size=1), effects=effect_list_strategy(min_size=1, max_size=5))
    @settings(max_examples=50)
    def test_extend_returns_new_instance(self, stream: Stream, effects: list[Effect]) -> None:
        """extend() should return a new Stream instance."""
        original_len = len(stream)

        new_stream = stream.extend(effects)

        assert stream is not new_stream
        assert len(stream) == original_len
        assert len(new_stream) == original_len + len(effects)

    @given(stream=stream_strategy(min_size=3))
    @settings(max_examples=50)
    def test_truncate_returns_new_instance(self, stream: Stream) -> None:
        """truncate_to() should return a new Stream (or self if no-op)."""
        position = len(stream) // 2
        original_len = len(stream)

        truncated = stream.truncate_to(position)

        # Original unchanged
        assert len(stream) == original_len

        # Truncated is either new or self (if position >= len)
        if position < original_len:
            assert truncated is not stream
            assert len(truncated) == position


# =============================================================================
# Property Tests: Sequence Ordering
# =============================================================================


class TestStreamSequenceOrdering:
    """Tests for Stream sequence ordering invariant."""

    @given(stream=stream_strategy(min_size=1, max_size=50))
    @settings(max_examples=100)
    def test_sequence_equals_index(self, stream: Stream) -> None:
        """Every layer's sequence number should equal its index."""
        for i, layer in enumerate(stream):
            assert layer.sequence == i, f"Layer at index {i} has sequence {layer.sequence}"

    @given(effects=effect_list_strategy(min_size=1, max_size=30))
    @settings(max_examples=50)
    def test_sequence_continuous_after_multiple_appends(self, effects: list[Effect]) -> None:
        """Sequence numbers should be continuous after multiple appends."""
        stream = Stream()
        for i, effect in enumerate(effects):
            stream = stream.append(effect)
            # Check latest layer
            assert stream[i].sequence == i
            # Check all layers still valid
            for j in range(i + 1):
                assert stream[j].sequence == j

    @given(stream=stream_strategy(min_size=5))
    @settings(max_examples=50)
    def test_truncate_preserves_sequence_numbers(self, stream: Stream) -> None:
        """Truncation should preserve sequence numbers of remaining effects."""
        position = len(stream) // 2
        truncated = stream.truncate_to(position)

        for i in range(len(truncated)):
            assert truncated[i].sequence == i
            # Effects should be the same as original
            assert truncated[i].effect == stream[i].effect


# =============================================================================
# Property Tests: Append-Only Ordering
# =============================================================================


class TestStreamAppendOnly:
    """Tests for Stream append-only invariant."""

    @given(effects=effect_list_strategy(min_size=2, max_size=20))
    @settings(max_examples=50)
    def test_order_preserved_across_appends(self, effects: list[Effect]) -> None:
        """Effects should appear in the order they were appended."""
        stream = Stream()
        for effect in effects:
            stream = stream.append(effect)

        for i, effect in enumerate(effects):
            assert stream[i].effect == effect

    @given(stream=stream_strategy(min_size=2), effect=effect_strategy())
    @settings(max_examples=50)
    def test_append_only_adds_to_end(self, stream: Stream, effect: Effect) -> None:
        """New effects should only appear at the end."""
        original_effects = [layer.effect for layer in stream]

        new_stream = stream.append(effect)

        # All original effects still present in same order
        for i, original in enumerate(original_effects):
            assert new_stream[i].effect == original

        # New effect at end
        assert new_stream[-1].effect == effect


# =============================================================================
# Property Tests: Truncation Bounds
# =============================================================================


class TestStreamTruncation:
    """Tests for Stream truncation invariant."""

    @given(stream=stream_strategy(min_size=1, max_size=30))
    @settings(max_examples=50)
    def test_truncate_keeps_exact_count(self, stream: Stream) -> None:
        """truncate_to(n) should keep exactly n effects."""
        for n in range(len(stream) + 1):
            truncated = stream.truncate_to(n)
            assert len(truncated) == n

    @given(stream=stream_strategy(min_size=5))
    @settings(max_examples=50)
    def test_truncate_keeps_first_n_effects(self, stream: Stream) -> None:
        """truncate_to(n) should keep the first n effects unchanged."""
        n = len(stream) // 2
        truncated = stream.truncate_to(n)

        for i in range(n):
            assert truncated[i].effect == stream[i].effect

    @given(stream=stream_strategy())
    @settings(max_examples=30)
    def test_truncate_beyond_length_returns_self(self, stream: Stream) -> None:
        """truncate_to(n) where n >= len should return self unchanged."""
        truncated = stream.truncate_to(len(stream) + 10)
        assert truncated is stream

    @given(stream=stream_strategy())
    @settings(max_examples=30)
    def test_truncate_negative_raises(self, stream: Stream) -> None:
        """truncate_to() with negative position should raise ValueError."""
        with pytest.raises(ValueError, match="position must be >= 0"):
            stream.truncate_to(-1)


# =============================================================================
# Property Tests: Serialization Roundtrip
# =============================================================================


class TestStreamSerialization:
    """Tests for Stream serialization roundtrip invariant."""

    @given(stream=stream_strategy(min_size=0, max_size=20))
    @settings(max_examples=50)
    def test_json_roundtrip_preserves_length(self, stream: Stream) -> None:
        """JSON serialize/deserialize should preserve stream length."""
        json_str = stream.to_json()
        restored = Stream.from_json(json_str)

        assert len(restored) == len(stream)

    @given(stream=stream_strategy(min_size=1, max_size=15))
    @settings(max_examples=50)
    def test_json_roundtrip_preserves_effect_types(self, stream: Stream) -> None:
        """JSON roundtrip should preserve effect types."""
        json_str = stream.to_json()
        restored = Stream.from_json(json_str)

        for i in range(len(stream)):
            original_type = type(stream[i].effect).__name__
            restored_type = type(restored[i].effect).__name__
            assert restored_type == original_type, f"Effect {i}: {original_type} != {restored_type}"

    @given(stream=stream_strategy(min_size=1, max_size=15))
    @settings(max_examples=50)
    def test_json_roundtrip_preserves_layer_metadata(self, stream: Stream) -> None:
        """JSON roundtrip should preserve layer metadata."""
        json_str = stream.to_json()
        restored = Stream.from_json(json_str)

        for i in range(len(stream)):
            original = stream[i]
            restored_layer = restored[i]

            assert restored_layer.sequence == original.sequence
            assert restored_layer.source_context == original.source_context
            assert restored_layer.scope_id == original.scope_id
            assert restored_layer.scope_depth == original.scope_depth

    @given(stream=stream_strategy(min_size=1, max_size=10))
    @settings(max_examples=30)
    def test_dicts_roundtrip_preserves_data(self, stream: Stream) -> None:
        """to_dicts/from_dicts roundtrip should preserve all data."""
        dicts = stream.to_dicts()
        restored = Stream.from_dicts(dicts)

        assert len(restored) == len(stream)
        for i in range(len(stream)):
            # Effect type preserved
            assert type(restored[i].effect).__name__ == type(stream[i].effect).__name__
            # Effect type field preserved
            assert restored[i].effect.effect_type == stream[i].effect.effect_type

    @given(stream=stream_strategy(min_size=1, max_size=10))
    @settings(max_examples=30)
    def test_json_is_valid_json(self, stream: Stream) -> None:
        """to_json() should produce valid JSON."""
        json_str = stream.to_json()
        # Should not raise
        parsed = json.loads(json_str)
        assert isinstance(parsed, list)
        assert len(parsed) == len(stream)


# =============================================================================
# Property Tests: Filter Composition
# =============================================================================


class TestStreamFilterComposition:
    """Tests for Stream filter composition (AND logic)."""

    @given(stream=stream_strategy(min_size=5, max_size=30))
    @settings(max_examples=30)
    def test_query_type_filter_only_returns_matching_types(self, stream: Stream) -> None:
        """Query with type filter should only return effects of that type."""
        results = list(stream.query(TaskStarted))

        for layer in results:
            assert isinstance(layer.effect, TaskStarted)

    @given(stream=stream_strategy(min_size=5, max_size=30))
    @settings(max_examples=30)
    def test_query_task_name_filter(self, stream: Stream) -> None:
        """Query with task_name should only return matching effects."""
        if not stream:
            return

        # Pick a task_name that exists
        existing_names = {layer.effect.task_name for layer in stream if layer.effect.task_name}
        if not existing_names:
            return

        task_name = next(iter(existing_names))
        results = list(stream.query(task_name=task_name))

        for layer in results:
            assert layer.effect.task_name == task_name

    @given(stream=stream_strategy(min_size=10, max_size=30))
    @settings(max_examples=30)
    def test_query_combined_filters_use_and_logic(self, stream: Stream) -> None:
        """Query with multiple filters should use AND logic."""
        # Find an effect that's both TaskStarted and has a task_name
        task_started_effects = [el for el in stream if isinstance(el.effect, TaskStarted)]
        if not task_started_effects:
            return

        target = task_started_effects[0]
        task_name = target.effect.task_name

        results = list(stream.query(TaskStarted, task_name=task_name))

        for layer in results:
            # Both conditions must be true (AND logic)
            assert isinstance(layer.effect, TaskStarted)
            assert layer.effect.task_name == task_name

    @given(stream=stream_strategy(min_size=1, max_size=20))
    @settings(max_examples=30)
    def test_count_equals_query_length(self, stream: Stream) -> None:
        """count() should equal len(list(query())) for same filters."""
        count_result = stream.count(TaskStarted)
        query_result = len(list(stream.query(TaskStarted)))

        assert count_result == query_result
