"""Generator-based direct evaluator for the source calculus (§02).

Each computation is interpreted as a Python generator that yields requests
of two kinds:

- `_PerformOp(effect_kind, payload)` when the source program executes
  `Perform(...)`;
- `_ResumeOp(value)` when a handler body executes `Resume(value)`.

A driver consumes these requests and dispatches them. The handler stack is
implicit in the recursive structure of `_run_gen_under_env` calls: each
`Handle` node introduces a fresh driver level. Generators give us
first-class delimited continuations: a paused generator IS the captured
continuation up to the matched delimiter.

Handles `Return`/`Let`/`Perform`/`Handle`/`Resume`/`Abort` with deep
handling as the default, plus opportunistic schema validation against an
optional `EffectRegistry`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from itertools import count
from typing import TYPE_CHECKING, Any

from shepherd_kernel_v3_reference.schemas import check
from shepherd_kernel_v3_reference.source.effects import EffectRegistry
from shepherd_kernel_v3_reference.source.handlers import (
    DynamicHandlerInstall,
    HandlerEnv,
    StaticHandlerInstall,
)
from shepherd_kernel_v3_reference.source.outcomes import (
    Completed,
    Continuation,
    ResumptionUsed,
    SourceOutcome,
    Suspended,
)
from shepherd_kernel_v3_reference.source.syntax import (
    Abort,
    Computation,
    Expr,
    Handle,
    Let,
    Lit,
    Perform,
    RecordExpr,
    Resume,
    Return,
    Var,
)
from shepherd_kernel_v3_reference.source.values import Env
from shepherd_kernel_v3_reference.source.wellformed import validate_handler_body, validate_program

if TYPE_CHECKING:
    from collections.abc import Generator, Iterator

# --- internal yield types ---------------------------------------------------


@dataclass(frozen=True)
class _PerformOp:
    effect_kind: str
    payload: Any


@dataclass(frozen=True)
class _ResumeOp:
    value: Any


_NO_VALUE: Any = object()


class _AbortSignal(Exception):
    """Internal nonlocal exit for handler-local `Abort(value)`."""

    def __init__(self, value: Any) -> None:
        super().__init__("handler abort")
        self.value = value


class AbortAfterResume(RuntimeError):
    """Raised when a handler completes via `Abort(value)` after the selected
    worker resumption has been invoked.

    §10 lists `resume(...); Abort(...)` among rejected histories: in the
    core, `Abort` is the no-prior-worker-resume short-circuit case. A
    handler that has already called the selected worker resumption may
    still complete with ordinary `Return(value)`, but not with `Abort`.
    """


# --- expression evaluation --------------------------------------------------


def eval_expr(expr: Expr, env: Env) -> Any:
    match expr:
        case Lit(value):
            return value
        case Var(name):
            return env.lookup(name)
        case RecordExpr(fields):
            return {name: eval_expr(value, env) for name, value in fields}
        case _:
            raise TypeError(f"unknown expression form: {expr!r}")


# --- evaluator --------------------------------------------------------------


@dataclass
class _DirectEvaluator:
    registry: EffectRegistry
    fresh: Iterator[int] = field(default_factory=count)

    # ---- public entry point ----

    def run(self, term: Computation, env: Env | None = None) -> SourceOutcome:
        if env is None:
            env = Env()
        validate_program(term)
        gen = self._eval_gen(term, env)
        return self._drive_top(gen)

    # ---- term -> generator ----

    def _eval_gen(self, term: Computation, env: Env) -> Generator[Any, Any, Any]:
        match term:
            case Return(expr):
                return eval_expr(expr, env)

            case Let(name=name, bound=bound, body=body):
                v = yield from self._eval_gen(bound, env)
                return (yield from self._eval_gen(body, env.extend(name, v)))

            case Perform(effect_kind=effect_kind, payload=payload_expr):
                payload = eval_expr(payload_expr, env)
                if effect_kind in self.registry:
                    sig = self.registry.lookup(effect_kind)
                    check(
                        sig.payload_schema,
                        payload,
                        context=f"perform({effect_kind!r}) payload",
                    )
                return (yield _PerformOp(effect_kind, payload))

            case Handle(body=body, handler_env=henv):
                inner_gen = self._eval_gen(body, env)
                return (yield from self._run_gen_under_env(inner_gen, henv, env))

            case Resume(value=value_expr):
                value = eval_expr(value_expr, env)
                return (yield _ResumeOp(value))

            case Abort(value=value_expr):
                # Abort is handler-local short-circuit completion. Source
                # erasure delivers the same value outward as handler Return,
                # but it skips the remaining handler-local continuation.
                raise _AbortSignal(eval_expr(value_expr, env))

            case _:
                raise TypeError(f"unknown computation form: {term!r}")

    # ---- drivers ----

    def _drive_top(self, gen: Generator[Any, Any, Any], initial: Any = _NO_VALUE) -> SourceOutcome:
        """Drive a top-level generator. Unmatched performs become Suspended."""
        sent = None if initial is _NO_VALUE else initial
        while True:
            try:
                op = gen.send(sent)
            except _AbortSignal as abort:
                raise RuntimeError("Abort(value) used outside any handler body") from abort
            except StopIteration as stop:
                return Completed(stop.value)

            if isinstance(op, _PerformOp):
                # Captured continuation: re-enter this same generator with the
                # supplied operation-result value. The generator's state IS the
                # captured worker continuation up to the top level.
                return Suspended(
                    op.effect_kind,
                    op.payload,
                    self._top_level_continuation(gen, op.effect_kind),
                )

            if isinstance(op, _ResumeOp):
                raise RuntimeError("Resume(value) used outside any handler body")

            raise RuntimeError(f"unexpected yield from generator: {op!r}")

    def _top_level_continuation(self, gen: Generator[Any, Any, Any], effect_kind: str) -> Continuation:
        def cont(value: Any) -> SourceOutcome:
            if effect_kind in self.registry:
                sig = self.registry.lookup(effect_kind)
                check(
                    sig.operation_result_schema,
                    value,
                    context=f"resume({effect_kind!r})",
                )
            return self._drive_top(gen, initial=value)

        return Continuation(cont)

    def _run_gen_under_env(
        self,
        gen: Generator[Any, Any, Any],
        henv: HandlerEnv,
        env: Env,
        initial: Any = _NO_VALUE,
    ) -> Generator[Any, Any, Any]:
        """Drive `gen`, dispatching matching performs against `henv`.

        Returns the value of the surrounding `Handle` expression: if `gen`
        completes without any matched perform, that completion value; if a
        handler runs and answers, the handler's answer (per §02's deep-handler
        equation `resume(v) = Handle(e[v], h)`).
        """
        sent = None if initial is _NO_VALUE else initial
        while True:
            try:
                op = gen.send(sent)
            except _AbortSignal as abort:
                raise RuntimeError("Abort(value) used outside any handler body") from abort
            except StopIteration as stop:
                return stop.value

            if isinstance(op, _PerformOp):
                install = henv.lookup(op.effect_kind)
                if install is None:
                    sent = yield op
                    continue
                handler_term, handler_env = self._handler_term_and_env(
                    install,
                    op.payload,
                    env,
                )
                validate_handler_body(handler_term)
                handler_gen = self._eval_gen(handler_term, handler_env)
                # Deep handling: this handler stays installed when its
                # resume runs the worker; that's why _run_handler passes
                # `henv` and the same `gen` back to `_run_gen_under_env`.
                handler_value = yield from self._run_handler(
                    handler_gen,
                    body_gen=gen,
                    henv=henv,
                    env=handler_env,
                    install=install,
                    performed_effect_kind=op.effect_kind,
                )
                return handler_value

            if isinstance(op, _ResumeOp):
                raise RuntimeError("Resume(value) used outside any handler body")

            raise RuntimeError(f"unexpected yield from generator: {op!r}")

    def _run_handler(
        self,
        handler_gen: Generator[Any, Any, Any],
        body_gen: Generator[Any, Any, Any],
        henv: HandlerEnv,
        env: Env,
        install: Any,
        performed_effect_kind: str,
    ) -> Generator[Any, Any, Any]:
        """Drive a selected handler's body generator.

        On `_ResumeOp(v)`, the worker generator is driven under `henv` with
        initial send `v`; the resulting worker R-value is sent back into the
        handler. On `_PerformOp(...)` (handler-side effect), propagate
        outward — handler-side effects run under the env active when the
        handler was selected, not against `henv` itself.

        On handler completion, the answer is checked against
        `install.handled_result_schema` (§10's handled-result typing law).
        """
        handler_sent: Any = None
        resume_used = False
        while True:
            try:
                op = handler_gen.send(handler_sent)
            except _AbortSignal as abort:
                if resume_used:
                    raise AbortAfterResume(
                        f"handler {install.handler_id!r} aborted after invoking "
                        "the selected worker resumption; "
                        "§10 rejects `resume(...); Abort(...)` in the core"
                    )
                check(
                    install.handled_result_schema,
                    abort.value,
                    context=f"handler({install.handler_id!r}) answer",
                )
                return abort.value
            except StopIteration as stop:
                check(
                    install.handled_result_schema,
                    stop.value,
                    context=f"handler({install.handler_id!r}) answer",
                )
                return stop.value

            if isinstance(op, _ResumeOp):
                if resume_used:
                    raise ResumptionUsed(f"resumption for handler {install.handler_id!r} already used")
                resume_used = True
                if performed_effect_kind in self.registry:
                    sig = self.registry.lookup(performed_effect_kind)
                    check(
                        sig.operation_result_schema,
                        op.value,
                        context=f"resume({performed_effect_kind!r})",
                    )
                handler_sent = yield from self._run_gen_under_env(body_gen, henv, env, initial=op.value)
                continue

            if isinstance(op, _PerformOp):
                handler_sent = yield op
                continue

            raise RuntimeError(f"unexpected yield from generator: {op!r}")

    def _handler_term_and_env(
        self,
        install: Any,
        payload: Any,
        env: Env,
    ) -> tuple[Computation, Env]:
        if isinstance(install, DynamicHandlerInstall):
            return install.body(payload), env
        if isinstance(install, StaticHandlerInstall):
            return install.body, env.extend(install.payload_name, payload)
        raise TypeError(f"unknown handler install: {install!r}")


def run(
    term: Computation,
    env: Env | None = None,
    registry: EffectRegistry | None = None,
) -> SourceOutcome:
    """Evaluate a closed source program. See module docstring for semantics."""
    return _DirectEvaluator(registry=registry or EffectRegistry()).run(term, env)
