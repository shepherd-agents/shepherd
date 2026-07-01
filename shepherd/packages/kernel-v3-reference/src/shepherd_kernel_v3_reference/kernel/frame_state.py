"""Evaluator continuation frame state for the kernel v3 machine."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Literal, Union

from shepherd_kernel_v3_reference.kernel.context import ExecutionContext

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

    from shepherd_kernel_v3_reference.kernel.continuation_objects import ContinuationStackCursor
    from shepherd_kernel_v3_reference.kernel.ir import HandlerInstallDef, Ref
    from shepherd_kernel_v3_reference.source.values import Env


@dataclass(frozen=True)
class BindFrame:
    binder_id: Ref
    env: Env
    context: ExecutionContext


@dataclass(frozen=True)
class HandlerFrame:
    handler_env_ref: Ref
    env: Env
    region_ref: Ref
    entry_context: ExecutionContext = field(default_factory=ExecutionContext)
    outer_context: ExecutionContext = field(default_factory=ExecutionContext)


@dataclass(frozen=True)
class HandlerReturnFrame:
    install: HandlerInstallDef
    selected_handler_frame: HandlerFrame
    handler_env: Env
    captured_kont: tuple[Frame, ...] = ()
    outer_kont: tuple[Frame, ...] = ()
    captured_state: KontState | None = None
    captured_frame_refs: tuple[Ref, ...] = ()
    captured_stack_ref: Ref | None = None
    selected_handler_frame_ref: Ref | None = None
    outer_state: KontState | None = None
    outer_frame_refs: tuple[Ref, ...] = ()
    outer_stack_ref: Ref | None = None
    worker_context: ExecutionContext = field(default_factory=ExecutionContext)
    handler_context: ExecutionContext = field(default_factory=ExecutionContext)
    outer_context: ExecutionContext = field(default_factory=ExecutionContext)
    declaration_ref: Ref | None = None
    selection_ref: Ref | None = None
    resumption_handle_ref: Ref | None = None
    selection_path_ref: Ref | None = None
    captured_continuation_ref: Ref | None = None
    outer_continuation_ref: Ref | None = None
    captured_continuation_control_ref: Ref | None = None
    outer_continuation_control_ref: Ref | None = None
    operation_result_schema_ref: Ref | None = None
    handled_result_schema_ref: Ref | None = None


@dataclass(frozen=True)
class ResumeReturnFrame:
    resume_ref: Ref | None
    selection_path_ref: Ref | None
    handler_continuation_ref: Ref | None
    handler_dynamic_tail_ref: Ref | None
    handler_return_frame: HandlerReturnFrame
    handler_continuation: tuple[Frame, ...] = ()
    handler_dynamic_tail: tuple[Frame, ...] = ()
    handler_continuation_state: KontState | None = None
    handler_continuation_frame_refs: tuple[Ref, ...] = ()
    handler_continuation_stack_ref: Ref | None = None
    handler_return_frame_ref: Ref | None = None
    handler_dynamic_tail_state: KontState | None = None
    handler_dynamic_tail_frame_refs: tuple[Ref, ...] = ()
    handler_dynamic_tail_stack_ref: Ref | None = None
    handler_context: ExecutionContext = field(default_factory=ExecutionContext)


Frame = Union[BindFrame, HandlerFrame, HandlerReturnFrame, ResumeReturnFrame]


class KontState:
    __slots__ = (
        "_cursor",
        "_depth",
        "_frame",
        "_frame_ref",
        "_frame_refs_cache",
        "_frames_cache",
        "_kind",
        "_left",
        "_right",
        "_suffix_cursors_cache",
        "_tail",
    )

    _cursor: ContinuationStackCursor
    _depth: int
    _frame: Frame | None
    _frame_ref: Ref | None
    _frame_refs_cache: tuple[Ref, ...] | None
    _frames_cache: tuple[Frame, ...] | None
    _kind: Literal["empty", "cons", "concat"]
    _left: KontState | None
    _right: KontState | None
    _suffix_cursors_cache: tuple[ContinuationStackCursor, ...] | None
    _tail: KontState | None

    def __init__(
        self,
        frames: tuple[Frame, ...],
        frame_refs: tuple[Ref, ...],
        suffix_cursors: tuple[ContinuationStackCursor, ...],
    ) -> None:
        if len(frames) != len(frame_refs) or len(suffix_cursors) != len(frames) + 1:
            raise ValueError("KontState frame/ref/cursor tuple lengths disagree")
        state = self.empty(suffix_cursors[-1])
        for index in reversed(range(len(frames))):
            state = self.cons(frames[index], frame_refs[index], suffix_cursors[index], state)
        self._copy_from(state)

    @classmethod
    def empty(cls, cursor: ContinuationStackCursor) -> KontState:
        state = cls.__new__(cls)
        state._kind = "empty"
        state._cursor = cursor
        state._depth = 0
        state._frame = None
        state._frame_ref = None
        state._tail = None
        state._left = None
        state._right = None
        state._frames_cache = ()
        state._frame_refs_cache = ()
        state._suffix_cursors_cache = (cursor,)
        return state

    @classmethod
    def cons(
        cls,
        frame: Frame,
        frame_ref: Ref,
        cursor: ContinuationStackCursor,
        tail: KontState,
    ) -> KontState:
        state = cls.__new__(cls)
        state._kind = "cons"
        state._cursor = cursor
        state._depth = tail.depth + 1
        state._frame = frame
        state._frame_ref = frame_ref
        state._tail = tail
        state._left = None
        state._right = None
        state._frames_cache = None
        state._frame_refs_cache = None
        state._suffix_cursors_cache = None
        return state

    @classmethod
    def concat(cls, left: KontState, right: KontState, cursor: ContinuationStackCursor) -> KontState:
        if left.is_empty:
            return right
        if right.is_empty:
            return left
        state = cls.__new__(cls)
        state._kind = "concat"
        state._cursor = cursor
        state._depth = left.depth + right.depth
        state._frame = None
        state._frame_ref = None
        state._tail = None
        state._left = left
        state._right = right
        state._frames_cache = None
        state._frame_refs_cache = None
        state._suffix_cursors_cache = None
        return state

    def _copy_from(self, other: KontState) -> None:
        self._kind = other._kind
        self._cursor = other._cursor
        self._depth = other._depth
        self._frame = other._frame
        self._frame_ref = other._frame_ref
        self._tail = other._tail
        self._left = other._left
        self._right = other._right
        self._frames_cache = other._frames_cache
        self._frame_refs_cache = other._frame_refs_cache
        self._suffix_cursors_cache = other._suffix_cursors_cache

    @property
    def cursor(self) -> ContinuationStackCursor:
        return self._cursor

    @property
    def depth(self) -> int:
        return self._depth

    @property
    def is_empty(self) -> bool:
        return self._depth == 0

    @property
    def frames(self) -> tuple[Frame, ...]:
        if self._frames_cache is None:
            self._frames_cache = tuple(frame for frame, _frame_ref in self._iter_frame_refs())
        return self._frames_cache

    @property
    def frame_refs(self) -> tuple[Ref, ...]:
        if self._frame_refs_cache is None:
            self._frame_refs_cache = tuple(frame_ref for _frame, frame_ref in self._iter_frame_refs())
        return self._frame_refs_cache

    @property
    def suffix_cursors(self) -> tuple[ContinuationStackCursor, ...]:
        if self._suffix_cursors_cache is None:
            if self._kind == "cons":
                self._suffix_cursors_cache = (self.cursor,) + self._require_tail().suffix_cursors
            else:
                raise RuntimeError("suffix cursor materialization is unavailable for concat continuations")
        return self._suffix_cursors_cache

    def __bool__(self) -> bool:
        return not self.is_empty

    def head(self) -> tuple[Frame, Ref] | None:
        if self._kind == "empty":
            return None
        if self._kind == "cons":
            if self._frame is None or self._frame_ref is None:
                raise RuntimeError("malformed continuation cons state")
            return self._frame, self._frame_ref
        if self._kind == "concat":
            left = self._require_left()
            return left.head()
        raise RuntimeError(f"unknown continuation state kind {self._kind!r}")

    def iter_frames(self) -> Iterator[Frame]:
        for frame, _frame_ref in self._iter_frame_refs():
            yield frame

    def tail_state(self, concat: Callable[[KontState, KontState], KontState]) -> KontState:
        if self._kind == "empty":
            raise IndexError("empty continuation has no tail")
        if self._kind == "cons":
            return self._require_tail()
        if self._kind == "concat":
            left_tail = self._require_left().tail_state(concat)
            return concat(left_tail, self._require_right())
        raise RuntimeError(f"unknown continuation state kind {self._kind!r}")

    def suffix(self, index: int) -> KontState:
        if index < 0:
            raise IndexError("continuation suffix index must be non-negative")
        if index == 0:
            return self
        if index > self.depth:
            raise IndexError("continuation suffix index out of range")
        if self._kind == "empty":
            return self
        if self._kind == "cons":
            return self._require_tail().suffix(index - 1)
        if self._kind == "concat":
            left = self._require_left()
            if index < left.depth:
                raise RuntimeError("partial concat suffix requires a runtime concat service")
            if index == left.depth:
                return self._require_right()
            return self._require_right().suffix(index - left.depth)
        raise RuntimeError(f"unknown continuation state kind {self._kind!r}")

    def _iter_frame_refs(self) -> Iterator[tuple[Frame, Ref]]:
        if self._kind == "empty":
            return
        if self._kind == "cons":
            if self._frame is None or self._frame_ref is None:
                raise RuntimeError("malformed continuation cons state")
            yield self._frame, self._frame_ref
            yield from self._require_tail()._iter_frame_refs()
            return
        if self._kind == "concat":
            yield from self._require_left()._iter_frame_refs()
            yield from self._require_right()._iter_frame_refs()
            return
        raise RuntimeError(f"unknown continuation state kind {self._kind!r}")

    def _require_tail(self) -> KontState:
        if self._tail is None:
            raise RuntimeError("malformed continuation cons tail")
        return self._tail

    def _require_left(self) -> KontState:
        if self._left is None:
            raise RuntimeError("malformed continuation concat left")
        return self._left

    def _require_right(self) -> KontState:
        if self._right is None:
            raise RuntimeError("malformed continuation concat right")
        return self._right


def _require_ref(value: Ref | None, context: str) -> Ref:
    if value is None:
        raise RuntimeError(f"{context} is required for continuation object projection")
    return value
