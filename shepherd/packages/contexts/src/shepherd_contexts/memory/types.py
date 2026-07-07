"""Data types for the memory context.

Observation/hint shapes for a :class:`MemoryBackend`, compatible with a typical
content-addressable memory substrate, without the framework depending on any
specific one.

Design notes
------------
- Hints are *advisory*: they surface into a run's system prompt but never enter
  the ``state(t) = fold(apply_effect, effects)`` derivation. A run's correctness
  depends only on its effect trace; recalled memory is logged (see
  :class:`~shepherd_contexts.memory.effects.MemoryRecalled`) for auditability,
  never treated as authoritative.
- ``digest`` is the backend's content/provenance address for a hint (e.g. an
  observation id / record digest). Recording it in the trace makes a recalled
  hint replay-auditable across consolidation epochs.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict

MemoryType = Literal["decision", "pattern", "discovery", "bugfix", "learning", "manual"]
"""Observation type taxonomy."""


class MemoryHint(BaseModel):
    """A single recalled advisory hint surfaced into a run."""

    model_config = ConfigDict(frozen=True)

    title: str
    content: str
    type: MemoryType = "learning"
    # Backend content/provenance address (observation id / digest).
    # Recorded in MemoryRecalled so a hint is replay-auditable.
    digest: str | None = None
    # Backend that produced this hint (e.g. "memory", an external store id).
    source: str = "memory"
    # Optional relevance score from the backend (0..1). Not load-bearing.
    score: float | None = None


class MemoryObservation(BaseModel):
    """A memory-worthy observation written at settlement / failure time.

    Shape mirrors a typical ``/observations`` payload so a backend can forward
    it unchanged. Written out-of-band — only after the trace is closed and the
    review gate has settled (select/release/discard) or a TaskFailed fired.
    """

    model_config = ConfigDict(frozen=True)

    type: MemoryType = "learning"
    title: str
    content: str
    project: str | None = None
    topic_key: str | None = None
    # Free-form provenance: the shepherd run/trace this observation came from.
    source: str | None = None


__all__ = ["MemoryHint", "MemoryObservation", "MemoryType"]
