"""Memory context effects.

``MemoryRecalled`` is the audit record for advisory memory surfaced into a run.
It is emitted by :class:`~shepherd_contexts.memory.context.MemoryContext` at
extract time and persisted in the effect trace, so ``shepherd run trace`` shows
exactly which recalled hints influenced a run — resolving the "the digest lies"
objection (a recall is part of the trace, not a hidden prompt mutation).

Provenance: each recalled hint's ``digest`` (the backend's content address) is
recorded, so a hint is replay-auditable across consolidation epochs.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal

from shepherd_core.effects import Effect

if TYPE_CHECKING:
    from collections.abc import Mapping


class MemoryRecalled(Effect):
    """Advisory memory was recalled and surfaced into a run's system prompt.

    Emitted once per task execution that binds a MemoryContext, regardless of
    whether the agent acted on the hints. Carries the query, the backend that
    answered, the project scope, and a compact provenance record of the hints
    (title + digest per hint) so the recall is auditable and replayable without
    re-running the backend.
    """

    effect_type: Literal["memory_recalled"] = "memory_recalled"

    # What was asked and who answered.
    query: str = ""
    backend: str = ""
    project: str | None = None

    # Compact provenance: per hint, its title and backend digest (if any).
    hint_count: int = 0
    hint_titles: tuple[str, ...] = ()
    hint_digests: tuple[str | None, ...] = ()


def get_effect_types() -> Mapping[str, type[Effect]]:
    """Return the explicit effect contributor surface for runtime decode."""
    return {"memory_recalled": MemoryRecalled}


__all__ = ["MemoryRecalled", "get_effect_types"]
