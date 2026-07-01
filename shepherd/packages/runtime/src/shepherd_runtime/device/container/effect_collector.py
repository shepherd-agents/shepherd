"""Runtime-owned effect collector for container execution."""

from __future__ import annotations

import dataclasses
import logging
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel
from shepherd_core.effects import (
    Effect,
    EffectTypeRegistry,
    ToolCallBatch,
    ToolCallCompleted,
    ToolCallRejected,
    ToolCallStarted,
)

from shepherd_runtime.effects import compose_effect_registry, decode_effect

logger = logging.getLogger(__name__)

_INTENT_EFFECT_TYPES = frozenset(
    {
        ToolCallStarted.model_fields["effect_type"].default,
        ToolCallCompleted.model_fields["effect_type"].default,
        ToolCallRejected.model_fields["effect_type"].default,
        ToolCallBatch.model_fields["effect_type"].default,
    }
)


@dataclass
class EffectCollector:
    """Minimal scope implementation for container execution."""

    _id: str = "container-collector"
    _collected_effects: list[Effect] = field(default_factory=list)
    _last_completed_intent_id: str | None = field(default=None)

    @property
    def id(self) -> str:
        """Identifier for effect attribution."""
        return self._id

    def emit(self, effect: Effect) -> None:
        """Emit an effect during execution."""
        self._collected_effects.append(effect)
        if effect.effect_type == "tool_call_completed":
            self._last_completed_intent_id = getattr(effect, "tool_call_id", None)
        elif effect.effect_type == "tool_call_batch":
            # Batch effects use batch_id as the causality anchor.
            # All filesystem effects get linked to the batch, not to
            # individual tool calls — honest about what we know.
            self._last_completed_intent_id = getattr(effect, "batch_id", None)

    def get_last_completed_intent_id(self) -> str | None:
        """Return tool_call_id of the most recently completed tool call."""
        return self._last_completed_intent_id

    def get_intent_effects(self) -> tuple[Effect, ...]:
        """Return collected IntentEffects."""
        return tuple(e for e in self._collected_effects if e.effect_type in _INTENT_EFFECT_TYPES)

    def get_lifecycle_effects(self) -> tuple[Effect, ...]:
        """Return collected lifecycle effects."""
        return tuple(e for e in self._collected_effects if e.effect_type not in _INTENT_EFFECT_TYPES)

    def get_all_effects(self) -> tuple[Effect, ...]:
        """Return all collected effects in emission order."""
        return tuple(self._collected_effects)

    def _serialize_effect(self, effect: Effect) -> dict[str, Any]:
        """Serialize a single effect to a dictionary."""
        if isinstance(effect, BaseModel):
            return effect.model_dump()

        logger.warning(
            "Non-Pydantic effect encountered during serialization: %s. Using fallback serialization.",
            type(effect).__name__,
        )

        if dataclasses.is_dataclass(effect) and not isinstance(effect, type):
            return dataclasses.asdict(effect)

        try:
            return dict(vars(effect))
        except TypeError:
            return {
                "effect_type": getattr(effect, "effect_type", "unknown"),
                "_fallback_repr": repr(effect),
            }

    def serialize_for_transport(self) -> dict[str, Any]:
        """Serialize collector state for transport across container boundary."""
        return {
            "collector_id": self._id,
            "last_completed_intent_id": self._last_completed_intent_id,
            "effects": [self._serialize_effect(e) for e in self._collected_effects],
        }

    @classmethod
    def deserialize_from_transport(
        cls,
        data: dict[str, Any],
        *,
        registry: EffectTypeRegistry | None = None,
    ) -> EffectCollector:
        """Reconstruct collector from transported data."""
        collector = cls(_id=data.get("collector_id", "container-collector"))
        collector._last_completed_intent_id = data.get("last_completed_intent_id")
        decode_registry = registry or compose_effect_registry()
        collector._collected_effects = [decode_effect(e, registry=decode_registry) for e in data.get("effects", [])]
        return collector

    def clear(self) -> None:
        """Clear all collected effects and reset state."""
        self._collected_effects.clear()
        self._last_completed_intent_id = None

    def __len__(self) -> int:
        """Return the number of collected effects."""
        return len(self._collected_effects)

    def __repr__(self) -> str:
        """Return a concise debugging representation."""
        intent_count = len(self.get_intent_effects())
        lifecycle_count = len(self.get_lifecycle_effects())
        return (
            f"EffectCollector(id={self._id!r}, effects={len(self)}, intent={intent_count}, lifecycle={lifecycle_count})"
        )


__all__ = [
    "_INTENT_EFFECT_TYPES",
    "EffectCollector",
]
