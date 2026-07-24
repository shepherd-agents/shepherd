"""Pluggable memory backends for :class:`~shepherd_contexts.memory.context.MemoryContext`.

The :class:`MemoryBackend` protocol is the seam: Shepherd knows how to *surface*
and *log* recalled memory; a backend decides where it comes from.
``InMemoryBackend`` is the deterministic default (no external dependencies,
ideal for tests).

This module ships only the generic, dependency-free SPI and a reference
in-memory backend. Concrete backends that talk to an external memory substrate
(e.g. a durable cross-session store) are provided out-of-tree — implement the
:class:`MemoryBackend` protocol and pass an instance to
``MemoryContext.create(...)``.
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Protocol, runtime_checkable

if TYPE_CHECKING:
    from .types import MemoryHint, MemoryObservation


@runtime_checkable
class MemoryBackend(Protocol):
    """Read/write SPI for an advisory memory substrate."""

    name: str

    def recall(
        self,
        query: str,
        *,
        project: str | None = None,
        n: int = 5,
    ) -> list[MemoryHint]:
        """Return up to ``n`` advisory hints relevant to ``query``.

        Must be total: never raise. A backend that cannot answer returns ``[]``.
        """
        ...

    def save(self, observation: MemoryObservation) -> str | None:
        """Persist a memory-worthy observation; return its id, or ``None``.

        Must be total: never raise. Out-of-band only — callers must have already
        settled the run (select/release/discard) or confirmed a TaskFailed.
        """
        ...


class InMemoryBackend:
    """Deterministic in-process backend. The default; ideal for tests.

    ``recall`` does naive case-insensitive substring matching of the query
    against hint title+content, returning the top-``n`` matches (stable order).
    ``save`` appends to the in-process store and returns a synthetic id.
    """

    def __init__(self, hints: list[MemoryHint] | None = None) -> None:
        self._hints: list[MemoryHint] = list(hints or [])
        self._saved: list[MemoryObservation] = []
        self._counter = 0

    @property
    def name(self) -> str:
        return "memory"

    def recall(
        self,
        query: str,
        *,
        project: str | None = None,
        n: int = 5,
    ) -> list[MemoryHint]:
        del project  # the naive backend does not partition by project
        if not query.strip():
            # No query: surface the most recent hints (recency-ish, stable).
            return list(self._hints[-n:])
        needle = query.lower()
        terms = needle.split()
        scored: list[tuple[int, int, MemoryHint]] = []
        for idx, hint in enumerate(self._hints):
            hay = (hint.title + " " + hint.content).lower()
            hits = sum(1 for term in terms if term in hay)
            if hits:
                scored.append((hits, -idx, hint))  # more hits first, then earlier
        scored.sort(key=lambda t: (t[0], t[1]), reverse=True)
        return [h for _, _, h in scored[:n]]

    def save(self, observation: MemoryObservation) -> str | None:
        self._counter += 1
        obs_id = f"inmem-{self._counter}"
        self._saved.append(observation)
        return obs_id

    # Test/diagnostics helpers -------------------------------------------------

    @property
    def saved(self) -> list[MemoryObservation]:
        """Observations written via ``save`` (test inspection)."""
        return list(self._saved)


__all__ = ["InMemoryBackend", "MemoryBackend"]
