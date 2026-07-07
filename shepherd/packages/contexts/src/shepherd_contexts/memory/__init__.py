"""Advisory memory context for Shepherd.

Surfaces cross-run memory into a task's system prompt as a *logged* effect
(:class:`MemoryRecalled`), so what influenced a run is auditable in the trace.
Memory is advisory-only — it never enters the effect-replay fold or justifies a
release. Backends are pluggable via the :class:`MemoryBackend` protocol;
:class:`InMemoryBackend` is the deterministic, dependency-free default.

Concrete backends that talk to an external memory substrate live out-of-tree:
implement :class:`MemoryBackend` and pass an instance to
``MemoryContext.create(...)``.

Quick Start
-----------
    from shepherd_contexts.memory import (
        InMemoryBackend,
        MemoryContext,
        MemoryHint,
    )

    backend = InMemoryBackend([MemoryHint(title="auth", content="use setup-token")])
    memory = MemoryContext.create(backend, query="claude auth", project="shepherd")

The write path is out-of-band: at settlement (select/discard) or on TaskFailed,
build observations from the trace via :func:`observations_from_effects` and
persist them through ``backend.save(...)``.
"""

from __future__ import annotations

from shepherd_contexts.memory.backend import (
    InMemoryBackend,
    MemoryBackend,
)
from shepherd_contexts.memory.context import MemoryContext
from shepherd_contexts.memory.effects import MemoryRecalled
from shepherd_contexts.memory.types import MemoryHint, MemoryObservation
from shepherd_contexts.memory.write import observations_from_effects

__all__ = [
    "InMemoryBackend",
    "MemoryBackend",
    "MemoryContext",
    "MemoryHint",
    "MemoryObservation",
    "MemoryRecalled",
    "observations_from_effects",
]
