"""Callable task metadata extraction for the syntax nucleus."""

from __future__ import annotations

import ast
import dis
import inspect
import textwrap
import time
from dataclasses import dataclass
from functools import update_wrapper
from typing import TYPE_CHECKING, Annotated, Any, Generic, TypeVar, get_args, get_origin, get_type_hints

from shepherd_runtime.effects import Match, Plan
from shepherd_runtime.sync import run_sync
from shepherd_runtime.task._source_state import reconstruction_source

from .delivery import (
    _deliver_async,
    build_task_trace,
    make_task_run_context,
    pop_task_run,
    push_task_run,
)
from .profiles import EffectSurfaceProfile
from .task_hooks import enter_task_execution_hooks
from .types import (
    AmbientWorldAccessRefused,
    AmbiguousTaskBody,
    DeliveryExhausted,
    DeliveryFailed,
    DeliveryStopped,
    Exhausted,
    Failed,
    Finished,
    Run,
    Stopped,
)

if TYPE_CHECKING:
    from collections.abc import Callable


T = TypeVar("T")


@dataclass(frozen=True)
class ParameterMetadata:
    """Metadata captured for one function-form task parameter."""

    name: str
    annotation: Any
    base_annotation: Any
    metadata: tuple[Any, ...]
    default: Any
    has_default: bool
    kind: inspect._ParameterKind


@dataclass(frozen=True)
class StructuralMay:
    """Structural ``@task(may=...)`` declaration metadata.

    This is declaration metadata only in Path A. Launch-spine code continues to
    read ``TaskMetadata.may`` for the coarse runtime profile.
    """

    declaration: object
    match: Match


@dataclass(frozen=True)
class TaskMetadata:
    """Metadata captured for a function-form task callable (CONTRACTS A4).

    The ``guidance`` and ``name`` fields are opaque per DECISIONS D10:
    ``@task(guidance=..., name=...)`` are accepted as keyword arguments
    from day one and stored here without interpretation. Plan 04's
    prompt-construction layer reads ``guidance`` to seed provider
    context; future plans may read ``name`` for cache keys,
    registration, etc. ``name`` is ``None`` when no override is given;
    consumers should fall back to ``qualname``.

    Per-parameter defaults live on ``parameters[*].default`` plus
    ``parameters[*].has_default`` rather than a separate ``defaults``
    tuple — the per-parameter shape carries strictly more information.
    """

    module: str
    qualname: str
    signature: inspect.Signature
    return_annotation: Any
    return_base_annotation: Any
    return_metadata: tuple[Any, ...]
    parameters: tuple[ParameterMetadata, ...]
    is_async: bool
    source: str | None
    guidance: str | None = None
    name: str | None = None
    may: EffectSurfaceProfile | None = None
    structural_may: StructuralMay | None = None
    docstring: str | None = None
    bodyless: bool = False
    # W3 tri-state: True when the body cannot be classified (source unavailable —
    # exec/REPL/notebook definition) AND the compiled body is empty-shaped, so a
    # delegating bodyless task would otherwise run to a silent None. Raises loud
    # at call time instead.
    body_ambiguous: bool = False


class CallableTask(Generic[T]):
    """Internal callable-task wrapper for function-form task authoring."""

    def __init__(self, fn: Any, metadata: TaskMetadata) -> None:
        self._fn = fn
        self.metadata = metadata
        update_wrapper(self, fn)

    @property
    def may(self) -> EffectSurfaceProfile | None:
        """Declared task effect surface, if explicitly provided."""
        return self.metadata.may

    @property
    def structural_may(self) -> StructuralMay | None:
        """Structural task effect-surface declaration, if explicitly provided."""
        return self.metadata.structural_may

    def __call__(self, *args: Any, **kwargs: Any) -> T:
        self._bind_call(args, kwargs)
        if self.metadata.is_async:
            return self._call_async(*args, **kwargs)  # type: ignore[return-value]
        return self._run_sync(*args, **kwargs).unwrap()

    def detailed(self, *args: Any, **kwargs: Any) -> Run[T]:
        self._bind_call(args, kwargs)
        if self.metadata.is_async:
            return self._run_async(*args, **kwargs)  # type: ignore[return-value]
        return self._run_sync(*args, **kwargs)

    def run(self, *args: Any, **kwargs: Any) -> Run[T]:
        """Canonical user-facing invocation (``v1-integration.md`` §4.2).

        The prelaunch nucleus path delegates to ``.detailed()`` — same return
        type, same shape. ``Run[T]`` already carries ``outcome`` and ``ref``;
        no ``.detailed()`` chaining needed.
        """
        self._bind_call(args, kwargs)
        return self.detailed(*args, **kwargs)

    def _bind_call(self, args: tuple[Any, ...], kwargs: dict[str, Any]) -> None:
        self.metadata.signature.bind(*args, **kwargs)

    async def _call_async(self, *args: Any, **kwargs: Any) -> T:
        return (await self._run_async(*args, **kwargs)).unwrap()

    async def _run_async(self, *args: Any, **kwargs: Any) -> Run[T]:
        from shepherd_runtime.trace.runtime import pop_trace_recorder, push_trace_recorder

        if self.metadata.bodyless or self.metadata.body_ambiguous:
            _refuse_ambient_handle_delivery(self.metadata)
        if self.metadata.body_ambiguous:
            raise AmbiguousTaskBody(_ambiguous_body_message(self.metadata))
        context = make_task_run_context(task_name=self.metadata.qualname, is_async=True)
        task_token = push_task_run(context)
        trace_token = push_trace_recorder(context.trace_recorder)
        start = time.perf_counter()
        try:
            with enter_task_execution_hooks(self.metadata, context):
                if self.metadata.bodyless:
                    value = await _deliver_async(
                        self.metadata.return_base_annotation,
                        goal=_bodyless_goal(self.metadata),
                        evidence=_bodyless_evidence(self.metadata, args, kwargs),
                        constraints=(),
                        limits=None,
                    )
                else:
                    value = await self._fn(*args, **kwargs)
            return _finished_run(value, context, start, tuple(context.artifacts))
        except Exception as exc:  # noqa: BLE001
            return _failed_run(exc, context, start, tuple(context.artifacts))
        finally:
            pop_trace_recorder(trace_token)
            pop_task_run(task_token)

    def _run_sync(self, *args: Any, **kwargs: Any) -> Run[T]:
        from shepherd_runtime.trace.runtime import pop_trace_recorder, push_trace_recorder

        if self.metadata.bodyless or self.metadata.body_ambiguous:
            _refuse_ambient_handle_delivery(self.metadata)
        if self.metadata.body_ambiguous:
            raise AmbiguousTaskBody(_ambiguous_body_message(self.metadata))
        context = make_task_run_context(task_name=self.metadata.qualname, is_async=False)
        task_token = push_task_run(context)
        trace_token = push_trace_recorder(context.trace_recorder)
        start = time.perf_counter()
        try:
            with enter_task_execution_hooks(self.metadata, context):
                if self.metadata.bodyless:
                    value = run_sync(
                        _deliver_async(
                            self.metadata.return_base_annotation,
                            goal=_bodyless_goal(self.metadata),
                            evidence=_bodyless_evidence(self.metadata, args, kwargs),
                            constraints=(),
                            limits=None,
                        )
                    )
                else:
                    value = self._fn(*args, **kwargs)
            return _finished_run(value, context, start, tuple(context.artifacts))
        except Exception as exc:  # noqa: BLE001
            return _failed_run(exc, context, start, tuple(context.artifacts))
        finally:
            pop_trace_recorder(trace_token)
            pop_task_run(task_token)


def task(
    fn: Any = None,
    /,
    *,
    guidance: str | None = None,
    name: str | None = None,
    may: object | None = None,
) -> Any:
    """Decorate a function as a syntax nucleus callable task.

    Both bare and parameterized usage are supported per DECISIONS
    D10::

        @task
        async def foo(...): ...

        @task(guidance="...")
        async def foo(...): ...

        @task(guidance="...", name="custom")
        async def foo(...): ...

    ``guidance`` and ``name`` are stored opaquely on ``TaskMetadata``.
    ``may`` accepts either the minimal launch-spine ``ReadOnly`` /
    ``Permissive`` profiles or structural ``Match`` / extractable ``Plan``
    declarations. Structural declarations are metadata-only in Path A.
    """
    surface, structural_may = _coerce_task_may(may)

    def _wrap(target: Any) -> CallableTask[Any]:
        metadata = extract_callable_task_metadata(
            target,
            guidance=guidance,
            name=name,
            may=surface,
            structural_may=structural_may,
        )
        return CallableTask(target, metadata)

    if fn is None:
        # Parameterized: @task(guidance=...)
        return _wrap
    if callable(fn) and not isinstance(fn, str):
        # Bare: @task
        return _wrap(fn)
    raise TypeError("@task expects a callable or a keyword-only invocation")


def extract_callable_task_metadata(
    fn: object,
    *,
    guidance: str | None = None,
    name: str | None = None,
    may: EffectSurfaceProfile | None = None,
    structural_may: StructuralMay | None = None,
) -> TaskMetadata:
    """Extract function-form task metadata without interpreting marker semantics."""
    if inspect.isclass(fn):
        raise TypeError("@task function-form metadata does not accept classes")
    if not callable(fn):
        raise TypeError("@task function-form metadata requires a callable")

    signature = inspect.signature(fn)
    try:
        hints = get_type_hints(fn, include_extras=True)
    except Exception as exc:
        task_label = getattr(fn, "__qualname__", repr(fn))
        raise TypeError(f"Could not resolve type hints for {task_label}") from exc

    if "return" not in hints or signature.return_annotation is inspect.Signature.empty:
        task_label = getattr(fn, "__qualname__", repr(fn))
        raise TypeError(f"Callable task {task_label} must declare a return annotation")

    parameters: list[ParameterMetadata] = []
    for param_name, parameter in signature.parameters.items():
        if parameter.annotation is inspect.Parameter.empty or param_name not in hints:
            task_label = getattr(fn, "__qualname__", repr(fn))
            raise TypeError(f"Callable task {task_label} parameter {param_name!r} must be annotated")
        base_annotation, metadata = _split_annotated(hints[param_name])
        parameters.append(
            ParameterMetadata(
                name=param_name,
                annotation=hints[param_name],
                base_annotation=base_annotation,
                metadata=metadata,
                default=parameter.default if parameter.default is not inspect.Parameter.empty else None,
                has_default=parameter.default is not inspect.Parameter.empty,
                kind=parameter.kind,
            )
        )

    docstring = inspect.getdoc(fn)
    body_classification = _classify_body(fn)
    bodyless = body_classification == "bodyless"
    if bodyless and not (guidance or docstring):
        task_label = getattr(fn, "__qualname__", repr(fn))
        raise TypeError(
            f"Bodyless callable task {task_label} must declare a docstring or guidance= to use as the model-call goal"
        )

    return_base, return_metadata = _split_annotated(hints["return"])
    return TaskMetadata(
        module=getattr(fn, "__module__", ""),
        qualname=getattr(fn, "__qualname__", getattr(fn, "__name__", repr(fn))),
        signature=signature,
        return_annotation=hints["return"],
        return_base_annotation=return_base,
        return_metadata=return_metadata,
        parameters=tuple(parameters),
        is_async=inspect.iscoroutinefunction(fn),
        source=_capture_source(fn),
        guidance=guidance,
        name=name,
        may=may,
        structural_may=structural_may,
        docstring=docstring,
        bodyless=bodyless,
        body_ambiguous=body_classification == "ambiguous",
    )


def _coerce_task_may(value: object | None) -> tuple[EffectSurfaceProfile | None, StructuralMay | None]:
    if value is None:
        return None, None
    if isinstance(value, EffectSurfaceProfile):
        return value, None
    if isinstance(value, Match):
        return None, StructuralMay(declaration=value, match=value)
    if isinstance(value, Plan):
        return None, StructuralMay(declaration=value, match=value.extract_may())
    raise TypeError("@task(may=...) accepts ReadOnly, Permissive, Match, or an extractable Plan")


def _split_annotated(annotation: Any) -> tuple[Any, tuple[Any, ...]]:
    if get_origin(annotation) is Annotated:
        args = get_args(annotation)
        return args[0], tuple(args[1:])
    return annotation, ()


def _capture_source(fn: Callable[..., Any]) -> str | None:
    captured = reconstruction_source.get()
    if captured is not None:
        return textwrap.dedent(captured)
    try:
        return textwrap.dedent(inspect.getsource(fn))
    except (OSError, TypeError):
        return None


def classify_task_body(fn: Callable[..., Any]) -> str:
    """Public tri-state body classifier: ``"bodyless"`` / ``"bodied"`` / ``"ambiguous"``.

    Exposed for the workspace-control registration bridge, which routes a
    ``__main__``-defined task by body kind (bodyless is self-contained and safe to
    capture at definition scope; a bodied script body is refused). Wraps the same
    classification the nucleus decorator uses.
    """
    return _classify_body(fn)


def _classify_body(fn: Callable[..., Any]) -> str:
    """Classify the task body: ``"bodyless"`` / ``"bodied"`` / ``"ambiguous"`` (W3 tri-state).

    A bodyless task delegates entirely to the model: the decorator synthesizes a
    single ``deliver(...)`` from the return annotation, the docstring/guidance, and
    the bound arguments. When source parses, the AST decides — a bodied REPL task
    with readable source is never misclassified. When source is unavailable
    (exec/REPL/notebook), docstring-only and ``return None`` bodies compile
    byte-identically, so discrimination is fundamentally ambiguous: an
    empty-shaped compiled body classifies ``"ambiguous"`` (raises loud at call
    time instead of running to a silent ``None``), while a non-trivial compiled
    body classifies ``"bodied"`` and runs.
    """
    source = _capture_source(fn)
    if source is not None:
        try:
            tree = ast.parse(source)
        except SyntaxError:
            return "ambiguous" if _bytecode_trivial(fn) else "bodied"
        func = next(
            (node for node in tree.body if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)),
            None,
        )
        if func is None:
            return "ambiguous" if _bytecode_trivial(fn) else "bodied"
        body = func.body
        if (
            body
            and isinstance(body[0], ast.Expr)
            and isinstance(body[0].value, ast.Constant)
            and isinstance(body[0].value.value, str)
        ):
            body = body[1:]
        if not body:
            return "bodyless"
        trivial = all(
            isinstance(stmt, ast.Pass)
            or (isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Constant) and stmt.value.value is Ellipsis)
            for stmt in body
        )
        return "bodyless" if trivial else "bodied"
    return "ambiguous" if _bytecode_trivial(fn) else "bodied"


# Opcodes an empty-shaped body may contain: prologue/bookkeeping plus loading and
# returning a constant (the docstring or None). Any other instruction means real
# work — classify bodied and run it. Errs toward "bodied" (today's behavior) for
# exotic trivial variants, never toward refusing a real body.
_TRIVIAL_BODY_OPNAMES = frozenset(
    {
        "RESUME",
        "NOP",
        "CACHE",
        "LOAD_CONST",
        "RETURN_CONST",
        "RETURN_VALUE",
        "RETURN_GENERATOR",
        "POP_TOP",
    }
)


def _bytecode_trivial(fn: Callable[..., Any]) -> bool:
    """Whether the compiled body is empty-shaped (docstring/``...``/``return None`` only)."""
    code = getattr(fn, "__code__", None)
    if code is None:
        return False
    for instruction in dis.get_instructions(code):
        if instruction.opname not in _TRIVIAL_BODY_OPNAMES:
            return False
        if instruction.opname in {"LOAD_CONST", "RETURN_CONST"}:
            value = instruction.argval
            if value is not None and value is not Ellipsis and not isinstance(value, str):
                return False
    return True


def _ambiguous_body_message(metadata: TaskMetadata) -> str:
    return (
        f"cannot introspect the body of task {metadata.qualname!r}: source is "
        "unavailable (exec/REPL/notebook definition) and the compiled body is "
        "empty-shaped, so a delegating bodyless task would silently return None. "
        "Move the task into an importable .py file."
    )


def _bodyless_goal(metadata: TaskMetadata) -> str:
    goal = metadata.docstring or metadata.guidance
    assert goal is not None  # enforced in extract_callable_task_metadata
    return goal


def _bodyless_evidence(metadata: TaskMetadata, args: tuple[Any, ...], kwargs: dict[str, Any]) -> tuple[Any, ...]:
    bound = metadata.signature.bind(*args, **kwargs)
    bound.apply_defaults()
    return tuple(bound.arguments.values())


def _refuse_ambient_handle_delivery(metadata: TaskMetadata) -> None:
    """The signature-directed placement refusal (dispatch side of the P-030 fence).

    An ambient model call has no enforcing monitor for a substrate grant: the
    grant would be silently erased into prompt evidence, and the model's answer
    would return as a typed result claiming world work it cannot have done.
    Refuse before any handler/provider dispatch, keyed on the annotations —
    never the passed values. Covers every ambient spelling by construction:
    ``task(...)``, ``task.run(...)``, and ``task.detailed(...)`` all funnel
    through the two ``_run_*`` bodies that call this guard.
    """
    from shepherd_core.schema import find_handle_annotation

    declared: list[str] = []
    for parameter in metadata.parameters:
        noun = find_handle_annotation(parameter.annotation)
        if noun is not None:
            declared.append(f"parameter {parameter.name!r} declares {getattr(noun, '__name__', noun)!r}")
    return_noun = find_handle_annotation(metadata.return_base_annotation)
    if return_noun is not None:
        declared.append(f"return slot declares {getattr(return_noun, '__name__', return_noun)!r}")
    if declared:
        raise AmbientWorldAccessRefused(
            f"task {metadata.qualname!r} declares world access in its signature "
            f"({'; '.join(declared)}); a bodyless ambient call cannot honor it. "
            "Run it through retained execution instead: workspace.run(...)"
        )


def _finished_run(
    value: T,
    context: object,
    start: float,
    artifacts: tuple[object, ...] = (),
) -> Run[T]:
    from .delivery import TaskRunContext

    assert isinstance(context, TaskRunContext)
    return Run(
        outcome=Finished(value),
        effects=(),
        artifacts=artifacts,
        usage=None,
        duration=time.perf_counter() - start,
        trace=build_task_trace(context),
        ref=context.ref,
    )


def _failed_run(
    exc: Exception,
    context: object,
    start: float,
    artifacts: tuple[object, ...] = (),
) -> Run[Any]:
    from .delivery import TaskRunContext

    assert isinstance(context, TaskRunContext)
    outcome = _exception_to_outcome(exc)
    return Run(
        outcome=outcome,
        effects=(),
        artifacts=artifacts,
        usage=None,
        duration=time.perf_counter() - start,
        trace=build_task_trace(context),
        ref=context.ref,
    )


def _exception_to_outcome(exc: Exception) -> Failed | Exhausted | Stopped:
    if isinstance(exc, DeliveryFailed):
        if isinstance(exc.run, Run) and isinstance(exc.run.outcome, Failed):
            return exc.run.outcome
        return Failed(error_type=type(exc).__name__, message=str(exc))
    if isinstance(exc, DeliveryExhausted):
        return Exhausted(reason=str(exc))
    if isinstance(exc, DeliveryStopped):
        return Stopped(reason=str(exc))
    return Failed(error_type=type(exc).__name__, message=str(exc))


__all__ = [
    "CallableTask",
    "ParameterMetadata",
    "StructuralMay",
    "TaskMetadata",
    "extract_callable_task_metadata",
    "task",
]
