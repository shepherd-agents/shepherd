"""MemoryContext: advisory cross-run memory as a logged, auditable effect.

A read-only execution context that recalls advisory hints from a
:class:`~shepherd_contexts.memory.backend.MemoryBackend` and surfaces them into
a run's system prompt. The recall is recorded as a
:class:`~shepherd_contexts.memory.effects.MemoryRecalled` effect in the trace, so
``shepherd run trace`` shows exactly what memory influenced a run.

Design (per the integration council's consensus):

- **Advisory only.** Memory never enters ``state(t) = fold(apply_effect, …)``.
  A run's correctness depends solely on its effect trace. Hints shape the
  prompt; they never justify a release or mutate execution state.
- **Logged, not injected invisibly.** ``MemoryRecalled`` is emitted at extract
  time with each hint's backend digest, so a recall is replay-auditable. This
  keeps the trace honest about what shaped the agent — the "the digest lies"
  failure mode is avoided because the recall is *in* the trace, not a hidden
  prompt mutation.
- **Eager recall.** The backend is consulted once at ``create()`` (side effects
  allowed there), so ``configure()`` stays pure — it only reads the already-
  recalled hints to build the prompt addition. The backend *name* is captured
  as a serializable field so ``MemoryRecalled.backend`` survives serialization
  (a PrivateAttr would be dropped, lying about provenance).
- **Pluggable backend.** ``InMemoryBackend`` (default, deterministic) or
  any backend implementing :class:`MemoryBackend`. Either degrades to empty.

Known limitations
-----------------
- ``MemoryRecalled`` is emitted at extract time (after execution). A run that
  fails *before* the extract phase (e.g. ``execute_sdk`` raises) will not emit
  one, so the dedicated audit record of which hints shaped that run is absent —
  though ``ContextConfigured`` still records that a memory binding was active.
  Emitting the recall before execution would require a framework hook to publish
  effects at configure/prepare time.
- Hints reach the prompt via ``ProviderBinding.system_prompt_additions``, the
  same channel every context uses. Any device/container execution mode that
  does not propagate ``system_prompt_additions`` (a framework-wide concern, not
  specific to memory) will omit the hints there; the in-process path is unaffected.

Example:
    from shepherd_contexts.memory import MemoryContext, InMemoryBackend, MemoryHint

    backend = InMemoryBackend([MemoryHint(title="...", content="...")])
    memory = MemoryContext.create(backend, query="how to auth claude", project="shepherd")

    with Scope() as scope:
        scope.bind("memory", memory)
        # ... run a task; its prompt carries the hints, its trace logs MemoryRecalled
"""

from __future__ import annotations

import hashlib
from typing import TYPE_CHECKING, Any, ClassVar, Self

from pydantic import BaseModel, ConfigDict
from shepherd_core.types import (
    ExecutionResult,
    ProviderBinding,
    ProviderCapabilities,
    ReversibilityLevel,
)
from shepherd_runtime.context import Bindable

from shepherd_contexts.memory.effects import MemoryRecalled
from shepherd_contexts.memory.types import MemoryHint

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path

    from shepherd_core.effects import Effect
    from shepherd_runtime.context import Sandbox

    from shepherd_contexts.memory.backend import MemoryBackend

_ADVISORY_HEADER = (
    "## Advisory memory (advisory only — verify before relying; never a release justification)"
)


class MemoryContext(BaseModel, Bindable):
    """Read-only advisory memory context backed by a :class:`MemoryBackend`."""

    __binding_name__: ClassVar[str] = "memory"

    model_config = ConfigDict(frozen=True, arbitrary_types_allowed=True)

    # Serializable recalled state (set eagerly by create()). backend_name is a
    # field (not a PrivateAttr) so MemoryRecalled.backend survives serialization.
    query: str = ""
    project: str | None = None
    max_hints: int = 5
    hints: tuple[MemoryHint, ...] = ()
    backend_name: str = "none"
    frozen_context_id: str | None = None

    @property
    def context_id(self) -> str:
        # NOTE: this is a QUERY-IDENTITY address (query|project|max_hints), not
        # a content address — two contexts with the same query but different
        # recalled hints collide. Replay-auditability lives in MemoryRecalled's
        # hint_digests, not here. See KVStoreContext for a content-addressed id.
        if self.frozen_context_id:
            return self.frozen_context_id
        return f"memory:{self._compute_hash()[:12]}"

    @property
    def reversibility(self) -> ReversibilityLevel:
        # Pure advisory input — no world mutation, mechanically a no-op to undo.
        return ReversibilityLevel.AUTO

    def __str__(self) -> str:
        """Invisible in the prompt body; hints travel via system_prompt_additions."""
        return ""

    def __repr__(self) -> str:
        n = len(self.hints)
        proj = f", project={self.project!r}" if self.project else ""
        return f"MemoryContext(query={self.query!r}, hints={n}{proj})"

    def _compute_hash(self) -> str:
        base = f"{self.query}|{self.project or ''}|{self.max_hints}"
        return hashlib.sha256(base.encode()).hexdigest()

    @classmethod
    def create(
        cls,
        backend: MemoryBackend | None,
        *,
        query: str = "",
        project: str | None = None,
        max_hints: int = 5,
    ) -> MemoryContext:
        """Eagerly recall and build a context.

        The backend is consulted here (side effects allowed) so the returned
        context's ``configure()`` can be pure. A ``None`` backend yields an
        empty (no-op) context.
        """
        ctx = cls(query=query, project=project, max_hints=max_hints)
        if backend is not None and query.strip():
            recalled = backend.recall(query, project=project, n=max_hints)
            object.__setattr__(ctx, "hints", tuple(recalled))
            object.__setattr__(ctx, "backend_name", backend.name)
        frozen_id = f"memory:{ctx._compute_hash()[:12]}"
        object.__setattr__(ctx, "frozen_context_id", frozen_id)
        return ctx

    # === ExecutionContext protocol ===

    def configure(
        self,
        capabilities: ProviderCapabilities | None = None,
    ) -> ProviderBinding:
        """Pure: surface recalled hints as an advisory system-prompt addition."""
        _ = capabilities  # memory is provider-agnostic
        return ProviderBinding(
            context_id=self.context_id,
            context_type="MemoryContext",
            context_description=f"Advisory memory ({len(self.hints)} hint(s))",
            visible=False,  # invisible in the prompt body; hints via additions
            system_prompt_additions=(self._prompt_block(),) if self.hints else (),
        )

    def prepare(self) -> MemoryContext:
        """No-op — recall happened eagerly in ``create()``."""
        return self

    def cleanup(self, error: Exception | None = None) -> None:
        """No resources to release."""
        _ = error

    def extract_effects(
        self,
        sandbox: Sandbox | None,
        result: ExecutionResult,
    ) -> Sequence[Effect]:
        """Emit one ``MemoryRecalled`` recording what was surfaced (pure)."""
        _ = sandbox, result
        return (
            MemoryRecalled(
                query=self.query,
                backend=self.backend_name,
                project=self.project,
                hint_count=len(self.hints),
                hint_titles=tuple(h.title for h in self.hints),
                hint_digests=tuple(h.digest for h in self.hints),
                context_id=self.context_id,
            ),
        )

    def apply_effect(self, effect: Effect) -> Self:
        """Memory is read-only advisory — it derives no state from effects."""
        _ = effect
        return self

    # === State serialization (device-boundary crossing) ===
    # Memory is host-side advisory; it does not cross device boundaries, so
    # transfer_bundle returns None (mirrors KVStoreContext). Defining these
    # explicitly avoids an AttributeError if a checkpoint/transfer path invokes
    # them, and makes the limitation explicit.

    def to_state(self) -> dict[str, Any]:
        """Serialize to a JSON-compatible state object."""
        return {
            "query": self.query,
            "project": self.project,
            "max_hints": self.max_hints,
            "hints": [h.model_dump(mode="json") for h in self.hints],
            "backend_name": self.backend_name,
            "frozen_context_id": self.frozen_context_id,
        }

    @classmethod
    def from_state(
        cls,
        state: Any,
        sandbox_path: Path | str | None = None,
    ) -> MemoryContext:
        """Reconstruct from state. Memory has no filesystem state, so the path is ignored."""
        _ = sandbox_path
        if not isinstance(state, dict):
            raise TypeError(f"MemoryContext.from_state expected dict, got {type(state).__name__}")
        hints = tuple(
            MemoryHint(**h) if isinstance(h, dict) else MemoryHint.model_validate(h)
            for h in state.get("hints", [])
        )
        return cls(
            query=state.get("query", ""),
            project=state.get("project"),
            max_hints=state.get("max_hints", 5),
            hints=hints,
            backend_name=state.get("backend_name", "none"),
            frozen_context_id=state.get("frozen_context_id"),
        )

    def transfer_bundle(self, scope: Any) -> None:
        """Memory stays on the host; it does not cross device boundaries."""
        return

    # === Helpers ===

    def _prompt_block(self) -> str:
        lines = [_ADVISORY_HEADER]
        for hint in self.hints:
            digest = f" (memory:{hint.digest})" if hint.digest else ""
            lines.append(f"- [{hint.type}] {hint.title}: {hint.content}{digest}")
        return "\n".join(lines)


__all__ = ["MemoryContext"]
