"""ScopeStack: consumer-side convenience over VcsCore.fork/merge/discard.

This is NOT a vcscore primitive -- it is a ~15-line consumer pattern.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from vcs_core.types import ScopeInfo
    from vcs_core.vcscore import VcsCore


class ScopeStack:
    """Stack-shaped ergonomics over VcsCore tree-shaped primitives."""

    def __init__(self, mg: VcsCore) -> None:
        self._mg = mg
        self._stack: list[ScopeInfo] = [mg.ground]

    def begin_scope(self, name: str, **hints: Any) -> ScopeInfo:
        """Fork a child scope from the current stack top."""
        scope = self._mg.fork(self._stack[-1], name, hints=hints or None)
        self._stack.append(scope)
        return scope

    def commit_scope(self) -> str:
        """Merge the current scope into its parent."""
        child = self._stack.pop()
        return self._mg.merge(child, self._stack[-1])

    def rollback_scope(self) -> str:
        """Discard the current scope."""
        child = self._stack.pop()
        return self._mg.discard(child)

    @property
    def current(self) -> ScopeInfo:
        """The current scope (top of stack)."""
        return self._stack[-1]

    @property
    def depth(self) -> int:
        """Current nesting depth (0 = ground only)."""
        return len(self._stack) - 1
