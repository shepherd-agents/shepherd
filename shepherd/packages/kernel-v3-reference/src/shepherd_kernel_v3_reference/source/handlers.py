"""Handler types: installations, environments, and decisions (§02, §03).

A `HandlerInstall` is a binding instance: it names handler code (`handler_id`)
and the handled-result schema for the extent it delimits. §03 distinguishes
binding instance from handler id so that repeated installations of the same
code remain unambiguous; we keep `handler_id` as a name, separate from the
install's identity (which is the install object itself).

Handler bodies have two representations. `DynamicHandlerInstall` stores a
Python builder `payload -> Computation` and is accepted only by the direct
source evaluator. `StaticHandlerInstall` stores a closed source fragment plus
the variable name used for the payload; this is the representation accepted by
kernel elaboration.

Handler results are `AnswerCompletion(ordinary | abort, value)`.
`Forward`/`Delay`/`Fork` control outcomes are §07 extensions and out of
scope here.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Callable

    from shepherd_kernel_v3_reference.schemas import Schema
    from shepherd_kernel_v3_reference.source.syntax import Computation


# --- handler installations --------------------------------------------------


@dataclass(frozen=True)
class DynamicHandlerInstall:
    effect_kind: str
    handler_id: str
    handled_result_schema: Schema
    body: Callable[[Any], Computation]

    def is_static(self) -> bool:
        return False


@dataclass(frozen=True)
class StaticHandlerInstall:
    effect_kind: str
    handler_id: str
    handled_result_schema: Schema
    payload_name: str
    body: Computation

    def is_static(self) -> bool:
        return True


HandlerInstall = DynamicHandlerInstall | StaticHandlerInstall


@dataclass(frozen=True)
class HandlerEnv:
    """A `Handle` node's installed bindings.

    Stored as an ordered tuple so iteration order is deterministic. Lookup
    here is local to a single `Handle` node; cross-frame nearest-handler
    lookup is the evaluator's job.
    """

    bindings: tuple[HandlerInstall, ...]

    def lookup(self, effect_kind: str) -> HandlerInstall | None:
        for inst in self.bindings:
            if inst.effect_kind == effect_kind:
                return inst
        return None


# --- handler decisions (answer-producing completions) -----------------------


@dataclass(frozen=True)
class AnswerCompletion:
    """An answer-producing handler completion.

    `kind="ordinary"` is `Answer(value)`; `kind="abort"` is `Abort(value)`.
    Both deliver `value` to the selected outer continuation under source
    erasure (§03); they differ in trace-visible intent only.
    """

    kind: str  # "ordinary" | "abort"
    value: Any

    def __post_init__(self) -> None:
        if self.kind not in ("ordinary", "abort"):
            raise ValueError(f"AnswerCompletion.kind must be 'ordinary' or 'abort', got {self.kind!r}")


# `HandlerDecision` would be a union once `ControlOutcome` (Forward/Delay/
# Fork) arrives; only `AnswerCompletion` is in scope here.
HandlerDecision = AnswerCompletion
