"""Source-level evaluation outcomes (§02).

§02 names::

    SourceOutcome ::= Completed(value)
                    | Suspended(effect_kind, payload, k)
                    | Delayed(reason, pending)
                    | Forked({ branch -> SourceOutcome })

The minimal Core-0/Core-A path uses `Completed` and `Suspended`. The
publication-core extensions additionally expose `Delayed` and `Forked` as
storage-free semantic outcomes.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Union

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping


class ResumptionUsed(RuntimeError):
    """Raised when a one-shot resumption is invoked more than once.

    Used both for handler-internal `Resume(value)` and for top-level
    `Suspended.continuation.apply(value)`. The single-root-branch fragment
    is one-shot, so any second use is a misuse.
    """


class Continuation:
    """Opaque source-level resumption.

    Wraps a Python callable; equality is identity, since two distinct CPS
    closures are not considered equal even if they would behave the same way
    on every input.

    One-shot. Calling :meth:`apply` a second time raises
    :class:`ResumptionUsed`. The underlying generator is mutated by the
    first call, so a second `apply` would silently produce nonsense
    otherwise.
    """

    __slots__ = ("_fn", "_used")

    def __init__(self, fn: Callable[[Any], SourceOutcome]) -> None:
        self._fn = fn
        self._used = False

    def apply(self, value: Any) -> SourceOutcome:
        if self._used:
            raise ResumptionUsed(
                "Suspended.continuation already applied once; the single-root-branch fragment is one-shot"
            )
        self._used = True
        return self._fn(value)

    def __repr__(self) -> str:
        return f"<Continuation@{id(self):x}>"


@dataclass(frozen=True)
class Completed:
    value: Any


@dataclass(frozen=True, eq=False)
class Suspended:
    """An unhandled `Perform` reached the top level.

    `continuation` is the source-level resumption; applying a value of the
    operation-result type continues the rest of the computation.

    `eq=False` because `Continuation` doesn't admit a useful equality;
    tests should compare `effect_kind` and `payload` field-by-field.
    """

    effect_kind: str
    payload: Any
    continuation: Continuation


@dataclass(frozen=True, eq=False)
class Delayed:
    """Terminal delay exported a pending continuation source."""

    reason: Any
    pending: Continuation


@dataclass(frozen=True)
class Forked:
    """Terminal fork produced branch-indexed source outcomes."""

    branches: Mapping[str, SourceOutcome]


SourceOutcome = Union[Completed, Suspended, Delayed, Forked]
