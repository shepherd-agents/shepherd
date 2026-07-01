"""Source-calculus syntax (§02).

The grammar is::

    c ::= Return(e)
        | Let(x, c1, c2)
        | Perform(effect_kind, payload)
        | Handle(c, handler_env)

    e ::= Lit(v) | Var(x)

§02 keeps `e ::= v | x`; we model both via tagged dataclasses so that
expressions can carry runtime values without ambiguity.

Region and authority annotations belong to §02's annotated kernel-source
layer, not to the pure source calculus, so `Handle` is `Handle(c, henv)` here.

The validated Core-A source surface covers `Return`/`Let`/`Perform`/`Handle`
plus the handler-body forms `Resume(value)` and `Abort(value)`.
Publication-experimental syntax lives in `shepherd_kernel_v3_reference.source.experimental` so the
default source surface remains Core-A.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Union

if TYPE_CHECKING:
    from shepherd_kernel_v3_reference.source.handlers import HandlerEnv

# --- expressions --------------------------------------------------------


@dataclass(frozen=True)
class Lit:
    value: Any


@dataclass(frozen=True)
class Var:
    name: str


@dataclass(frozen=True)
class RecordExpr:
    """Record expression with expression-valued fields.

    The pure source calculus keeps expressions small, but publication examples
    need to build structured effect payloads from resumed values without
    falling back to host-language callbacks.
    """

    fields: tuple[tuple[str, Expr], ...]


Expr = Union[Lit, Var, RecordExpr]


# --- computations -------------------------------------------------------


@dataclass(frozen=True)
class Return:
    expr: Expr


@dataclass(frozen=True)
class Let:
    name: str
    bound: Computation
    body: Computation


@dataclass(frozen=True)
class Perform:
    effect_kind: str
    payload: Expr


@dataclass(frozen=True)
class Handle:
    body: Computation
    handler_env: HandlerEnv


@dataclass(frozen=True)
class Resume:
    """Handler-body callable resume (§02).

    `resume(value)` is a source-calculus form available only inside a
    handler body. Evaluating it invokes the captured worker continuation
    with `value` and returns the worker's R-value back to the handler.
    Out-of-handler use is a runtime error.
    """

    value: Expr


@dataclass(frozen=True)
class Abort:
    """Handler-side explicit short-circuit completion (§02, §03).

    `Abort(value)` is `AnswerCompletion(kind=abort, value=value)`. It is a
    handler-local short-circuit completion: under source erasure it delivers
    `value` to the selected outer continuation like an ordinary `Return(value)`
    reaching the handler return frame, but it skips the remaining handler-local
    continuation.
    """

    value: Expr


CoreComputation = Union[
    Return,
    Let,
    Perform,
    Handle,
    Resume,
    Abort,
]


Computation = CoreComputation


# Forward import to avoid a cycle: HandlerEnv lives in `handlers.py` because
# it carries handler installations that ultimately reference computations.
