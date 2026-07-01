"""Content-addressed continuation DAG objects for the runtime trace profile."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field, fields, is_dataclass
from math import isfinite
from typing import TYPE_CHECKING, Any, Literal, Protocol, TypeAlias, Union, cast

from shepherd_kernel_v3_reference.kernel.continuations import CONTINUATION_IMAGE_KINDS, ContinuationImageKind
from shepherd_kernel_v3_reference.kernel.refs import content_ref

if TYPE_CHECKING:
    from shepherd_kernel_v3_reference.kernel.context import ExecutionContext
    from shepherd_kernel_v3_reference.kernel.ir import Ref

CONTINUATION_OBJECT_SCHEMA_VERSION = "shepherd_kernel_v3_reference.continuation-object.v5"
CONTINUATION_CONTROL_DAG_SCHEMA_VERSION = "shepherd_kernel_v3_reference.continuation-control-dag.v1"

JsonValue: TypeAlias = Any
ContinuationKind: TypeAlias = ContinuationImageKind
ContinuationFrameChildRole: TypeAlias = Literal["stack", "frame", "env"]
FrameKind: TypeAlias = Literal[
    "bind",
    "handler",
    "handler-return",
    "resume-return",
    "terminal-result",
]


class ContinuationObjectValidationError(ValueError):
    """Raised when continuation object evidence is malformed."""


class _FrozenJsonDict(dict[str, Any]):
    """Dict-shaped immutable mapping so JSON encoders still see an object."""

    __slots__ = ()

    def _blocked(self, *args: object, **kwargs: object) -> None:
        raise TypeError("ContinuationObject JSON mappings are immutable")

    __setitem__ = _blocked
    __delitem__ = _blocked
    clear = _blocked
    pop = _blocked
    popitem = _blocked  # type: ignore[assignment]
    setdefault = _blocked
    update = _blocked
    __ior__ = _blocked  # type: ignore[assignment]


@dataclass(frozen=True)
class ContinuationFrameSummary:
    required_schema_refs: tuple[Ref, ...] = ()
    code_identity_refs: tuple[Ref, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "required_schema_refs", _sorted_ref_tuple(self.required_schema_refs))
        object.__setattr__(self, "code_identity_refs", _sorted_ref_tuple(self.code_identity_refs))


@dataclass(frozen=True)
class ContinuationStackSummary:
    depth: int = 0
    required_schema_refs: tuple[Ref, ...] = ()
    code_identity_refs: tuple[Ref, ...] = ()

    def __post_init__(self) -> None:
        if self.depth < 0:
            raise ValueError("ContinuationStackSummary.depth must be non-negative")
        object.__setattr__(self, "required_schema_refs", _sorted_ref_tuple(self.required_schema_refs))
        object.__setattr__(self, "code_identity_refs", _sorted_ref_tuple(self.code_identity_refs))


@dataclass(frozen=True, kw_only=True)
class ContinuationRoot:
    program_ref: Ref
    branch_ref: Ref
    branch_scope_ref: Ref | None
    position: Literal["value"]
    continuation_kind: ContinuationKind
    execution_context_ref: Ref
    execution_context: Mapping[str, JsonValue]
    result_schema_ref: Ref | None
    stack_ref: Ref
    object_schema_version: str = CONTINUATION_OBJECT_SCHEMA_VERSION
    object_type: Literal["root"] = "root"

    def __post_init__(self) -> None:
        _check_object_schema_version(self.object_schema_version)
        if self.object_type != "root":
            raise ValueError("ContinuationRoot.object_type must be 'root'")
        if self.position != "value":
            raise ValueError("ContinuationRoot.position must be 'value'")
        if self.continuation_kind not in CONTINUATION_IMAGE_KINDS:
            raise ValueError(f"unknown ContinuationRoot.continuation_kind: {self.continuation_kind!r}")
        object.__setattr__(
            self,
            "execution_context",
            _freeze_mapping_value(self.execution_context, context="ContinuationRoot.execution_context"),
        )


@dataclass(frozen=True, kw_only=True)
class ContinuationEmptyStack:
    summary: ContinuationStackSummary = field(default_factory=ContinuationStackSummary)
    object_schema_version: str = CONTINUATION_OBJECT_SCHEMA_VERSION
    object_type: Literal["empty-stack"] = "empty-stack"

    def __post_init__(self) -> None:
        _check_object_schema_version(self.object_schema_version)
        if self.object_type != "empty-stack":
            raise ValueError("ContinuationEmptyStack.object_type must be 'empty-stack'")
        if self.summary != ContinuationStackSummary():
            raise ValueError("ContinuationEmptyStack.summary must be empty")


@dataclass(frozen=True, kw_only=True)
class ContinuationStackNode:
    head_frame_ref: Ref
    tail_stack_ref: Ref
    summary: ContinuationStackSummary
    object_schema_version: str = CONTINUATION_OBJECT_SCHEMA_VERSION
    object_type: Literal["stack-node"] = "stack-node"

    def __post_init__(self) -> None:
        _check_object_schema_version(self.object_schema_version)
        if self.object_type != "stack-node":
            raise ValueError("ContinuationStackNode.object_type must be 'stack-node'")


@dataclass(frozen=True, kw_only=True)
class ContinuationStackConcat:
    prefix_stack_ref: Ref
    tail_stack_ref: Ref
    summary: ContinuationStackSummary
    object_schema_version: str = CONTINUATION_OBJECT_SCHEMA_VERSION
    object_type: Literal["stack-concat"] = "stack-concat"

    def __post_init__(self) -> None:
        _check_object_schema_version(self.object_schema_version)
        if self.object_type != "stack-concat":
            raise ValueError("ContinuationStackConcat.object_type must be 'stack-concat'")


@dataclass(frozen=True, kw_only=True)
class ContinuationEnvEmpty:
    depth: int = 0
    object_schema_version: str = CONTINUATION_OBJECT_SCHEMA_VERSION
    object_type: Literal["env-empty"] = "env-empty"

    def __post_init__(self) -> None:
        _check_object_schema_version(self.object_schema_version)
        if self.object_type != "env-empty":
            raise ValueError("ContinuationEnvEmpty.object_type must be 'env-empty'")
        if self.depth != 0:
            raise ValueError("ContinuationEnvEmpty.depth must be 0")


@dataclass(frozen=True, kw_only=True)
class ContinuationEnvNode:
    parent_env_ref: Ref
    name: str
    value: JsonValue
    depth: int
    object_schema_version: str = CONTINUATION_OBJECT_SCHEMA_VERSION
    object_type: Literal["env-node"] = "env-node"

    def __post_init__(self) -> None:
        _check_object_schema_version(self.object_schema_version)
        if self.object_type != "env-node":
            raise ValueError("ContinuationEnvNode.object_type must be 'env-node'")
        if not isinstance(self.name, str):
            raise TypeError("ContinuationEnvNode.name must be a string")
        if self.depth <= 0:
            raise ValueError("ContinuationEnvNode.depth must be positive")
        object.__setattr__(
            self,
            "value",
            _freeze_json_compatible(self.value, context="ContinuationEnvNode.value"),
        )


@dataclass(frozen=True, kw_only=True)
class BindFramePayload:
    binder_ref: Ref
    env_ref: Ref
    context_ref: Ref
    context: Mapping[str, JsonValue]
    frame_kind: Literal["bind"] = "bind"

    def __post_init__(self) -> None:
        _check_frame_kind(self.frame_kind, "bind")
        object.__setattr__(
            self,
            "context",
            _freeze_mapping_value(self.context, context="BindFramePayload.context"),
        )


@dataclass(frozen=True, kw_only=True)
class HandlerFramePayload:
    handler_env_ref: Ref
    handler_env_def_ref: Ref
    region_ref: Ref
    env_ref: Ref
    entry_context_ref: Ref
    entry_context: Mapping[str, JsonValue]
    outer_context_ref: Ref
    outer_context: Mapping[str, JsonValue]
    frame_kind: Literal["handler"] = "handler"

    def __post_init__(self) -> None:
        _check_frame_kind(self.frame_kind, "handler")
        object.__setattr__(
            self,
            "entry_context",
            _freeze_mapping_value(self.entry_context, context="HandlerFramePayload.entry_context"),
        )
        object.__setattr__(
            self,
            "outer_context",
            _freeze_mapping_value(self.outer_context, context="HandlerFramePayload.outer_context"),
        )


@dataclass(frozen=True, kw_only=True)
class HandlerReturnFramePayload:
    captured_stack_ref: Ref
    selected_handler_frame_ref: Ref
    outer_stack_ref: Ref
    install_ref: Ref
    install_def_ref: Ref
    handler_binding_env_ref: Ref
    worker_context_ref: Ref
    worker_context: Mapping[str, JsonValue]
    handler_context_ref: Ref
    handler_context: Mapping[str, JsonValue]
    outer_context_ref: Ref
    outer_context: Mapping[str, JsonValue]
    declaration_ref: Ref
    selection_ref: Ref
    resumption_handle_ref: Ref
    selection_path_ref: Ref
    captured_continuation_control_ref: Ref
    outer_continuation_control_ref: Ref
    operation_result_schema_ref: Ref | None
    handled_result_schema_ref: Ref
    frame_kind: Literal["handler-return"] = "handler-return"

    def __post_init__(self) -> None:
        _check_frame_kind(self.frame_kind, "handler-return")
        object.__setattr__(
            self,
            "worker_context",
            _freeze_mapping_value(self.worker_context, context="HandlerReturnFramePayload.worker_context"),
        )
        object.__setattr__(
            self,
            "handler_context",
            _freeze_mapping_value(self.handler_context, context="HandlerReturnFramePayload.handler_context"),
        )
        object.__setattr__(
            self,
            "outer_context",
            _freeze_mapping_value(self.outer_context, context="HandlerReturnFramePayload.outer_context"),
        )


@dataclass(frozen=True, kw_only=True)
class ResumeReturnFramePayload:
    resume_ref: Ref
    selection_path_ref: Ref
    handler_continuation_stack_ref: Ref
    handler_return_frame_ref: Ref
    handler_dynamic_tail_stack_ref: Ref
    handler_context_ref: Ref
    handler_context: Mapping[str, JsonValue]
    frame_kind: Literal["resume-return"] = "resume-return"

    def __post_init__(self) -> None:
        _check_frame_kind(self.frame_kind, "resume-return")
        object.__setattr__(
            self,
            "handler_context",
            _freeze_mapping_value(self.handler_context, context="ResumeReturnFramePayload.handler_context"),
        )


@dataclass(frozen=True, kw_only=True)
class TerminalResultFramePayload:
    resume_ref: Ref
    selection_ref: Ref
    selection_path_ref: Ref
    outer_stack_ref: Ref
    delivery_tail_stack_ref: Ref
    branch_ref: Ref
    frame_kind: Literal["terminal-result"] = "terminal-result"

    def __post_init__(self) -> None:
        _check_frame_kind(self.frame_kind, "terminal-result")


ContinuationFramePayload = Union[
    BindFramePayload,
    HandlerFramePayload,
    HandlerReturnFramePayload,
    ResumeReturnFramePayload,
    TerminalResultFramePayload,
]


@dataclass(frozen=True, kw_only=True)
class ContinuationFrameNode:
    frame_kind: FrameKind
    summary: ContinuationFrameSummary
    payload: ContinuationFramePayload
    object_schema_version: str = CONTINUATION_OBJECT_SCHEMA_VERSION
    object_type: Literal["frame"] = "frame"

    def __post_init__(self) -> None:
        _check_object_schema_version(self.object_schema_version)
        if self.object_type != "frame":
            raise ValueError("ContinuationFrameNode.object_type must be 'frame'")
        if self.frame_kind != self.payload.frame_kind:
            raise ValueError("ContinuationFrameNode.frame_kind must match payload.frame_kind")


ContinuationObject = Union[
    ContinuationRoot,
    ContinuationEmptyStack,
    ContinuationStackNode,
    ContinuationStackConcat,
    ContinuationEnvEmpty,
    ContinuationEnvNode,
    ContinuationFrameNode,
]


@dataclass(frozen=True, kw_only=True)
class ContinuationControlIdentity:
    program_ref: Ref
    branch_ref: Ref
    branch_scope_ref: Ref | None
    position: Literal["value"]
    stack_ref: Ref
    control_schema_version: str = CONTINUATION_CONTROL_DAG_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.control_schema_version != CONTINUATION_CONTROL_DAG_SCHEMA_VERSION:
            raise ValueError(
                "ContinuationControlIdentity.control_schema_version must be "
                f"{CONTINUATION_CONTROL_DAG_SCHEMA_VERSION!r}"
            )
        if self.position != "value":
            raise ValueError("ContinuationControlIdentity.position must be 'value'")


@dataclass(frozen=True)
class ContinuationStackCursor:
    """Incremental handle for the current linked-stack root."""

    stack_ref: Ref
    summary: ContinuationStackSummary


@dataclass
class ContinuationProjectionStats:
    """Counters used to keep the K1a direct path honest in tests."""

    full_stack_tuple_scans: int = 0
    reachable_summary_walks: int = 0


@dataclass
class _ContinuationBuilderDiagnostics:
    """Private diagnostics for builder-local cache behavior."""

    stack_cursor_cache_hits: int = 0
    stack_cursor_cache_misses: int = 0


class ContinuationObjectStore(Protocol):
    def put(self, obj: ContinuationObject) -> Ref: ...
    def get(self, ref: Ref) -> ContinuationObject: ...
    def contains(self, ref: Ref) -> bool: ...
    def items(self) -> Iterable[tuple[Ref, ContinuationObject]]: ...
    def snapshot(self, roots: Iterable[Ref] | None = None) -> Mapping[Ref, ContinuationObject]: ...


class InMemoryContinuationObjectStore:
    """Content-addressed in-memory continuation object store."""

    def __init__(self) -> None:
        self._objects: dict[Ref, ContinuationObject] = {}

    def put(self, obj: ContinuationObject) -> Ref:
        ref = continuation_object_ref(obj)
        existing = self._objects.get(ref)
        if existing is not None and continuation_object_to_json(existing) != continuation_object_to_json(obj):
            raise RuntimeError(f"continuation object ref collision: {ref!r}")
        self._objects[ref] = obj
        return ref

    def get(self, ref: Ref) -> ContinuationObject:
        return self._objects[ref]

    def contains(self, ref: Ref) -> bool:
        return ref in self._objects

    def items(self) -> Iterable[tuple[Ref, ContinuationObject]]:
        return tuple((ref, self._objects[ref]) for ref in sorted(self._objects))

    def snapshot(self, roots: Iterable[Ref] | None = None) -> Mapping[Ref, ContinuationObject]:
        refs: set[Ref]
        if roots is None:
            refs = set(self._objects)
        else:
            refs = set()
            for root_ref in roots:
                if not isinstance(self.get(root_ref), ContinuationRoot):
                    raise TypeError(f"continuation snapshot root {root_ref!r} is not a ContinuationRoot")
                self._collect_reachable_iterative(root_ref, refs)
        return {ref: self._objects[ref] for ref in sorted(refs)}

    def _collect_reachable_iterative(self, ref: Ref, refs: set[Ref]) -> None:
        work = [ref]
        while work:
            current_ref = work.pop()
            if current_ref in refs:
                continue
            obj = self.get(current_ref)
            refs.add(current_ref)
            work.extend(continuation_object_child_refs(obj))


class ContinuationObjectBuilder:
    """Incremental constructor for continuation DAG roots and stack nodes."""

    def __init__(
        self,
        store: ContinuationObjectStore | None = None,
        stats: ContinuationProjectionStats | None = None,
    ) -> None:
        self.store = store or InMemoryContinuationObjectStore()
        self.stats = stats or ContinuationProjectionStats()
        self._diagnostics = _ContinuationBuilderDiagnostics()
        self._stack_cursor_cache: dict[tuple[Ref, Ref], ContinuationStackCursor] = {}
        self._stack_concat_cache: dict[tuple[Ref, Ref], ContinuationStackCursor] = {}
        empty = ContinuationEmptyStack()
        self.empty_stack_ref = self.store.put(empty)
        self.empty_stack = ContinuationStackCursor(
            stack_ref=self.empty_stack_ref,
            summary=empty.summary,
        )
        self.empty_env_ref = self.store.put(ContinuationEnvEmpty())

    def put_frame(self, payload: ContinuationFramePayload) -> Ref:
        summary = self.frame_summary(payload)
        return self.store.put(
            ContinuationFrameNode(
                frame_kind=payload.frame_kind,
                summary=summary,
                payload=payload,
            )
        )

    def put_env_node(self, *, parent_env_ref: Ref, name: str, value: JsonValue, depth: int) -> Ref:
        parent = self._expect_env(parent_env_ref)
        expected_depth = parent.depth + 1
        if depth != expected_depth:
            raise ValueError(f"env node depth {depth} does not match parent depth {expected_depth}")
        return self.store.put(
            ContinuationEnvNode(
                parent_env_ref=parent_env_ref,
                name=name,
                value=value,
                depth=depth,
            )
        )

    def push_frame(self, frame_ref: Ref, tail: ContinuationStackCursor) -> ContinuationStackCursor:
        self._expect_frame(frame_ref)
        tail_obj = self._expect_stack_cursor(tail)
        cache_key = (frame_ref, tail.stack_ref)
        cached = self._stack_cursor_cache.get(cache_key)
        if cached is not None:
            self._expect_stack_cursor(cached)
            self._diagnostics.stack_cursor_cache_hits += 1
            return cached
        self._diagnostics.stack_cursor_cache_misses += 1
        summary = ContinuationStackSummary(
            depth=tail_obj.summary.depth + 1,
        )
        stack_ref = self.store.put(
            ContinuationStackNode(
                head_frame_ref=frame_ref,
                tail_stack_ref=tail.stack_ref,
                summary=summary,
            )
        )
        cursor = ContinuationStackCursor(stack_ref=stack_ref, summary=summary)
        self._stack_cursor_cache[cache_key] = cursor
        return cursor

    def concat_stack(
        self,
        prefix: ContinuationStackCursor,
        tail: ContinuationStackCursor,
    ) -> ContinuationStackCursor:
        prefix_obj = self._expect_stack_cursor(prefix)
        tail_obj = self._expect_stack_cursor(tail)
        if prefix_obj.summary.depth == 0:
            return tail
        if tail_obj.summary.depth == 0:
            return prefix
        cache_key = (prefix.stack_ref, tail.stack_ref)
        cached = self._stack_concat_cache.get(cache_key)
        if cached is not None:
            self._expect_stack_cursor(cached)
            return cached
        summary = _concat_stack_summary(prefix_obj.summary, tail_obj.summary)
        stack_ref = self.store.put(
            ContinuationStackConcat(
                prefix_stack_ref=prefix.stack_ref,
                tail_stack_ref=tail.stack_ref,
                summary=summary,
            )
        )
        cursor = ContinuationStackCursor(stack_ref=stack_ref, summary=summary)
        self._stack_concat_cache[cache_key] = cursor
        return cursor

    def put_root(
        self,
        stack: ContinuationStackCursor,
        *,
        program_ref: Ref,
        branch_ref: Ref,
        branch_scope_ref: Ref | None,
        continuation_kind: ContinuationKind,
        execution_context_ref: Ref,
        execution_context: Mapping[str, JsonValue],
        result_schema_ref: Ref | None,
    ) -> Ref:
        self._expect_stack_cursor(stack)
        return self.store.put(
            ContinuationRoot(
                program_ref=program_ref,
                branch_ref=branch_ref,
                branch_scope_ref=branch_scope_ref,
                position="value",
                continuation_kind=continuation_kind,
                execution_context_ref=execution_context_ref,
                execution_context=execution_context,
                result_schema_ref=result_schema_ref,
                stack_ref=stack.stack_ref,
            )
        )

    def put_control_identity(
        self,
        stack: ContinuationStackCursor,
        *,
        program_ref: Ref,
        branch_ref: Ref,
        branch_scope_ref: Ref | None,
    ) -> Ref:
        self._expect_stack_cursor(stack)
        return continuation_control_identity_ref(
            ContinuationControlIdentity(
                program_ref=program_ref,
                branch_ref=branch_ref,
                branch_scope_ref=branch_scope_ref,
                position="value",
                stack_ref=stack.stack_ref,
            )
        )

    def frame_summary(self, payload: ContinuationFramePayload) -> ContinuationFrameSummary:
        required_schema_refs: set[Ref] = set()
        code_identity_refs: set[Ref] = set()

        if isinstance(payload, BindFramePayload):
            code_identity_refs.add(payload.binder_ref)
        elif isinstance(payload, HandlerFramePayload):
            code_identity_refs.add(payload.handler_env_def_ref)
        elif isinstance(payload, HandlerReturnFramePayload):
            code_identity_refs.add(payload.install_def_ref)
            if payload.operation_result_schema_ref is not None:
                required_schema_refs.add(payload.operation_result_schema_ref)
            required_schema_refs.add(payload.handled_result_schema_ref)
        elif isinstance(payload, ResumeReturnFramePayload | TerminalResultFramePayload):
            pass
        else:
            raise TypeError(f"unknown continuation frame payload: {payload!r}")

        for child_ref, child_role in continuation_frame_payload_child_roles(payload):
            if child_role == "stack":
                self._expect_stack(child_ref)
            elif child_role == "frame":
                self._expect_frame(child_ref)
            elif child_role == "env":
                self._expect_env(child_ref)
            else:
                raise TypeError(f"unknown continuation child role: {child_role!r}")

        return ContinuationFrameSummary(
            required_schema_refs=tuple(required_schema_refs),
            code_identity_refs=tuple(code_identity_refs),
        )

    def _expect_frame(self, ref: Ref) -> ContinuationFrameNode:
        obj = self.store.get(ref)
        if not isinstance(obj, ContinuationFrameNode):
            raise TypeError(f"continuation object {ref!r} is not a frame")
        return obj

    def _expect_stack(self, ref: Ref) -> ContinuationEmptyStack | ContinuationStackNode | ContinuationStackConcat:
        obj = self.store.get(ref)
        if not isinstance(obj, ContinuationEmptyStack | ContinuationStackNode | ContinuationStackConcat):
            raise TypeError(f"continuation object {ref!r} is not a stack")
        return obj

    def _expect_stack_cursor(
        self, cursor: ContinuationStackCursor
    ) -> ContinuationEmptyStack | ContinuationStackNode | ContinuationStackConcat:
        obj = self._expect_stack(cursor.stack_ref)
        if obj.summary != cursor.summary:
            raise ValueError(f"ContinuationStackCursor.summary does not match stored stack object {cursor.stack_ref!r}")
        return obj

    def _expect_env(self, ref: Ref) -> ContinuationEnvEmpty | ContinuationEnvNode:
        obj = self.store.get(ref)
        if not isinstance(obj, ContinuationEnvEmpty | ContinuationEnvNode):
            raise TypeError(f"continuation object {ref!r} is not an env")
        return obj


class KernelContinuationObjectProjector:
    """Compatibility projector from current evaluator frame tuples to DAG objects.

    This class is intentionally not the K1b hot path. It exists to let K1a
    compare object evidence against the current evaluator without changing the
    refs emitted into trace records.
    """

    def __init__(
        self,
        evaluator: Any,
        builder: ContinuationObjectBuilder | None = None,
    ) -> None:
        self.evaluator = evaluator
        self.builder = builder or ContinuationObjectBuilder()
        self.root_refs: list[Ref] = []
        self._frame_cache: dict[int, tuple[Any, Ref]] = {}
        self._stack_cache: dict[tuple[Ref, ...], ContinuationStackCursor] = {(): self.builder.empty_stack}

    @property
    def store(self) -> ContinuationObjectStore:
        return self.builder.store

    @property
    def stats(self) -> ContinuationProjectionStats:
        return self.builder.stats

    def project_root(
        self,
        kont: tuple[Any, ...],
        *,
        continuation_kind: ContinuationKind,
        context: ExecutionContext,
        result_schema_ref: Ref | None = None,
    ) -> Ref:
        stack = self.project_stack(kont)
        root_ref = self.builder.put_root(
            stack,
            program_ref=self.evaluator.program_ref,
            branch_ref=self.evaluator._state.branch_ref,
            branch_scope_ref=self.evaluator._state.branch_scope_ref,
            continuation_kind=continuation_kind,
            execution_context_ref=self.evaluator._context_ref(context),
            execution_context=self.evaluator._context_payload(context),
            result_schema_ref=result_schema_ref,
        )
        self.root_refs.append(root_ref)
        return root_ref

    def project_control_ref(self, kont: tuple[Any, ...]) -> Ref:
        stack = self.project_stack(kont)
        return self.builder.put_control_identity(
            stack,
            program_ref=self.evaluator.program_ref,
            branch_ref=self.evaluator._state.branch_ref,
            branch_scope_ref=self.evaluator._state.branch_scope_ref,
        )

    def project_stack(self, kont: tuple[Any, ...]) -> ContinuationStackCursor:
        self.stats.full_stack_tuple_scans += 1
        frame_refs = tuple(self.project_frame(frame) for frame in kont)
        cached = self._stack_cache.get(frame_refs)
        if cached is not None:
            return cached

        cursor = self.builder.empty_stack
        for frame_ref in reversed(frame_refs):
            cursor = self.builder.push_frame(frame_ref, cursor)
        self._stack_cache[frame_refs] = cursor
        return cursor

    def project_frame(self, frame: Any) -> Ref:
        cache_key = id(frame)
        cached = self._frame_cache.get(cache_key)
        if cached is not None:
            cached_frame, cached_ref = cached
            if cached_frame is frame:
                return cached_ref
        ref = self.builder.put_frame(self._frame_payload(frame))
        self._frame_cache[cache_key] = (frame, ref)
        return ref

    def _frame_payload(self, frame: Any) -> ContinuationFramePayload:
        from shepherd_kernel_v3_reference.kernel.frame_state import (
            BindFrame,
            HandlerFrame,
            HandlerReturnFrame,
            ResumeReturnFrame,
        )

        if isinstance(frame, BindFrame):
            return BindFramePayload(
                binder_ref=self.evaluator._binder_ref(frame.binder_id),
                env_ref=self.evaluator._env_ref(frame.env),
                context_ref=self.evaluator._context_ref(frame.context),
                context=self.evaluator._context_payload(frame.context),
            )
        if isinstance(frame, HandlerFrame):
            return HandlerFramePayload(
                handler_env_ref=frame.handler_env_ref,
                handler_env_def_ref=self.evaluator._handler_env_def_ref(frame.handler_env_ref),
                region_ref=frame.region_ref,
                env_ref=self.evaluator._env_ref(frame.env),
                entry_context_ref=self.evaluator._context_ref(frame.entry_context),
                entry_context=self.evaluator._context_payload(frame.entry_context),
                outer_context_ref=self.evaluator._context_ref(frame.outer_context),
                outer_context=self.evaluator._context_payload(frame.outer_context),
            )
        if isinstance(frame, HandlerReturnFrame):
            return HandlerReturnFramePayload(
                captured_stack_ref=self.project_stack(frame.captured_kont).stack_ref,
                selected_handler_frame_ref=self.project_frame(frame.selected_handler_frame),
                outer_stack_ref=self.project_stack(frame.outer_kont).stack_ref,
                install_ref=frame.install.install_ref,
                install_def_ref=self.evaluator._install_ref(frame.install),
                declaration_ref=_require_ref(frame.declaration_ref, "HandlerReturnFrame.declaration_ref"),
                selection_ref=_require_ref(frame.selection_ref, "HandlerReturnFrame.selection_ref"),
                resumption_handle_ref=_require_ref(
                    frame.resumption_handle_ref,
                    "HandlerReturnFrame.resumption_handle_ref",
                ),
                selection_path_ref=_require_ref(frame.selection_path_ref, "HandlerReturnFrame.selection_path_ref"),
                captured_continuation_control_ref=(
                    frame.captured_continuation_control_ref or self.project_control_ref(frame.captured_kont)
                ),
                outer_continuation_control_ref=(
                    frame.outer_continuation_control_ref or self.project_control_ref(frame.outer_kont)
                ),
                handler_binding_env_ref=self.evaluator._env_ref(frame.handler_env),
                worker_context_ref=self.evaluator._context_ref(frame.worker_context),
                worker_context=self.evaluator._context_payload(frame.worker_context),
                handler_context_ref=self.evaluator._context_ref(frame.handler_context),
                handler_context=self.evaluator._context_payload(frame.handler_context),
                outer_context_ref=self.evaluator._context_ref(frame.outer_context),
                outer_context=self.evaluator._context_payload(frame.outer_context),
                operation_result_schema_ref=frame.operation_result_schema_ref,
                handled_result_schema_ref=_require_ref(
                    frame.handled_result_schema_ref,
                    "HandlerReturnFrame.handled_result_schema_ref",
                ),
            )
        if isinstance(frame, ResumeReturnFrame):
            return ResumeReturnFramePayload(
                resume_ref=_require_ref(frame.resume_ref, "ResumeReturnFrame.resume_ref"),
                selection_path_ref=_require_ref(frame.selection_path_ref, "ResumeReturnFrame.selection_path_ref"),
                handler_continuation_stack_ref=self.project_stack(frame.handler_continuation).stack_ref,
                handler_return_frame_ref=self.project_frame(frame.handler_return_frame),
                handler_dynamic_tail_stack_ref=self.project_stack(frame.handler_dynamic_tail).stack_ref,
                handler_context_ref=self.evaluator._context_ref(frame.handler_context),
                handler_context=self.evaluator._context_payload(frame.handler_context),
            )
        raise TypeError(f"unknown continuation frame: {frame!r}")


def continuation_object_ref(obj: ContinuationObject) -> Ref:
    return content_ref("continuation-object", continuation_object_payload(obj))


def continuation_control_identity_ref(identity: ContinuationControlIdentity) -> Ref:
    return content_ref("continuation-control", continuation_control_identity_payload(identity))


def continuation_control_identity_payload(identity: ContinuationControlIdentity) -> dict[str, Any]:
    return _dataclass_to_payload(identity)


def continuation_control_identity_to_json(identity: ContinuationControlIdentity) -> dict[str, Any]:
    return cast("dict[str, Any]", _jsonify(continuation_control_identity_payload(identity)))


def continuation_object_payload(obj: ContinuationObject) -> dict[str, Any]:
    if isinstance(obj, ContinuationFrameNode):
        return {
            "object_schema_version": obj.object_schema_version,
            "object_type": obj.object_type,
            "frame_kind": obj.frame_kind,
            "summary": _dataclass_to_payload(obj.summary),
            "payload": _dataclass_to_payload(obj.payload),
        }
    return _dataclass_to_payload(obj)


def continuation_object_to_json(obj: ContinuationObject) -> dict[str, Any]:
    return cast("dict[str, Any]", _jsonify(continuation_object_payload(obj)))


def continuation_object_from_json(data: Mapping[str, Any]) -> ContinuationObject:
    object_type = _require_str(data, "object_type")
    if object_type == "root":
        return ContinuationRoot(
            object_schema_version=_require_str(data, "object_schema_version"),
            object_type="root",
            program_ref=_require_str(data, "program_ref"),
            branch_ref=_require_str(data, "branch_ref"),
            branch_scope_ref=_require_optional_str(data, "branch_scope_ref"),
            position="value",
            continuation_kind=_require_str(data, "continuation_kind"),  # type: ignore[arg-type]
            execution_context_ref=_require_str(data, "execution_context_ref"),
            execution_context=_require_mapping(data, "execution_context"),
            result_schema_ref=_require_optional_str(data, "result_schema_ref"),
            stack_ref=_require_str(data, "stack_ref"),
        )
    if object_type == "empty-stack":
        return ContinuationEmptyStack(
            object_schema_version=_require_str(data, "object_schema_version"),
            object_type="empty-stack",
            summary=_stack_summary_from_json(_require_mapping(data, "summary")),
        )
    if object_type == "stack-node":
        return ContinuationStackNode(
            object_schema_version=_require_str(data, "object_schema_version"),
            object_type="stack-node",
            head_frame_ref=_require_str(data, "head_frame_ref"),
            tail_stack_ref=_require_str(data, "tail_stack_ref"),
            summary=_stack_summary_from_json(_require_mapping(data, "summary")),
        )
    if object_type == "stack-concat":
        return ContinuationStackConcat(
            object_schema_version=_require_str(data, "object_schema_version"),
            object_type="stack-concat",
            prefix_stack_ref=_require_str(data, "prefix_stack_ref"),
            tail_stack_ref=_require_str(data, "tail_stack_ref"),
            summary=_stack_summary_from_json(_require_mapping(data, "summary")),
        )
    if object_type == "env-empty":
        return ContinuationEnvEmpty(
            object_schema_version=_require_str(data, "object_schema_version"),
            object_type="env-empty",
            depth=_require_int(data, "depth"),
        )
    if object_type == "env-node":
        return ContinuationEnvNode(
            object_schema_version=_require_str(data, "object_schema_version"),
            object_type="env-node",
            parent_env_ref=_require_str(data, "parent_env_ref"),
            name=_require_str(data, "name"),
            value=data["value"],
            depth=_require_int(data, "depth"),
        )
    if object_type == "frame":
        payload = _frame_payload_from_json(_require_mapping(data, "payload"))
        return ContinuationFrameNode(
            object_schema_version=_require_str(data, "object_schema_version"),
            object_type="frame",
            frame_kind=_require_str(data, "frame_kind"),  # type: ignore[arg-type]
            summary=_frame_summary_from_json(_require_mapping(data, "summary")),
            payload=payload,
        )
    raise ContinuationObjectValidationError(f"unknown continuation object_type: {object_type!r}")


def continuation_object_child_refs(obj: ContinuationObject) -> tuple[Ref, ...]:
    if isinstance(obj, ContinuationRoot):
        return (obj.stack_ref, _binding_env_ref(obj.execution_context))
    if isinstance(obj, ContinuationStackNode):
        return (obj.head_frame_ref, obj.tail_stack_ref)
    if isinstance(obj, ContinuationStackConcat):
        return (obj.prefix_stack_ref, obj.tail_stack_ref)
    if isinstance(obj, ContinuationEnvNode):
        return (obj.parent_env_ref,)
    if isinstance(obj, ContinuationFrameNode):
        return continuation_frame_payload_child_refs(obj.payload)
    if isinstance(obj, ContinuationEmptyStack | ContinuationEnvEmpty):
        return ()
    raise TypeError(f"unknown continuation object: {obj!r}")


def continuation_frame_payload_child_refs(payload: ContinuationFramePayload) -> tuple[Ref, ...]:
    return tuple(ref for ref, _role in continuation_frame_payload_child_roles(payload))


def continuation_frame_payload_child_roles(
    payload: ContinuationFramePayload,
) -> tuple[tuple[Ref, ContinuationFrameChildRole], ...]:
    if isinstance(payload, HandlerReturnFramePayload):
        return (
            (payload.captured_stack_ref, "stack"),
            (payload.selected_handler_frame_ref, "frame"),
            (payload.outer_stack_ref, "stack"),
            (payload.handler_binding_env_ref, "env"),
            (_binding_env_ref(payload.worker_context), "env"),
            (_binding_env_ref(payload.handler_context), "env"),
            (_binding_env_ref(payload.outer_context), "env"),
        )
    if isinstance(payload, ResumeReturnFramePayload):
        return (
            (payload.handler_continuation_stack_ref, "stack"),
            (payload.handler_return_frame_ref, "frame"),
            (payload.handler_dynamic_tail_stack_ref, "stack"),
            (_binding_env_ref(payload.handler_context), "env"),
        )
    if isinstance(payload, TerminalResultFramePayload):
        return ((payload.outer_stack_ref, "stack"), (payload.delivery_tail_stack_ref, "stack"))
    if isinstance(payload, BindFramePayload):
        return ((payload.env_ref, "env"), (_binding_env_ref(payload.context), "env"))
    if isinstance(payload, HandlerFramePayload):
        return (
            (payload.env_ref, "env"),
            (_binding_env_ref(payload.entry_context), "env"),
            (_binding_env_ref(payload.outer_context), "env"),
        )
    raise TypeError(f"unknown continuation frame payload: {payload!r}")


def _object_summary(obj: ContinuationObject) -> ContinuationFrameSummary | ContinuationStackSummary:
    if isinstance(
        obj, ContinuationFrameNode | ContinuationEmptyStack | ContinuationStackNode | ContinuationStackConcat
    ):
        return obj.summary
    if isinstance(obj, ContinuationRoot):
        raise TypeError("root objects are not frame-payload children")
    if isinstance(obj, ContinuationEnvEmpty | ContinuationEnvNode):
        raise TypeError("env objects do not contribute continuation summaries")
    raise TypeError(f"unknown continuation object: {obj!r}")


def _frame_payload_from_json(data: Mapping[str, Any]) -> ContinuationFramePayload:
    frame_kind = _require_str(data, "frame_kind")
    if frame_kind == "bind":
        return BindFramePayload(
            binder_ref=_require_str(data, "binder_ref"),
            env_ref=_require_str(data, "env_ref"),
            context_ref=_require_str(data, "context_ref"),
            context=_require_mapping(data, "context"),
        )
    if frame_kind == "handler":
        return HandlerFramePayload(
            handler_env_ref=_require_str(data, "handler_env_ref"),
            handler_env_def_ref=_require_str(data, "handler_env_def_ref"),
            region_ref=_require_str(data, "region_ref"),
            env_ref=_require_str(data, "env_ref"),
            entry_context_ref=_require_str(data, "entry_context_ref"),
            entry_context=_require_mapping(data, "entry_context"),
            outer_context_ref=_require_str(data, "outer_context_ref"),
            outer_context=_require_mapping(data, "outer_context"),
        )
    if frame_kind == "handler-return":
        return HandlerReturnFramePayload(
            captured_stack_ref=_require_str(data, "captured_stack_ref"),
            selected_handler_frame_ref=_require_str(data, "selected_handler_frame_ref"),
            outer_stack_ref=_require_str(data, "outer_stack_ref"),
            install_ref=_require_str(data, "install_ref"),
            install_def_ref=_require_str(data, "install_def_ref"),
            handler_binding_env_ref=_require_str(data, "handler_binding_env_ref"),
            worker_context_ref=_require_str(data, "worker_context_ref"),
            worker_context=_require_mapping(data, "worker_context"),
            handler_context_ref=_require_str(data, "handler_context_ref"),
            handler_context=_require_mapping(data, "handler_context"),
            outer_context_ref=_require_str(data, "outer_context_ref"),
            outer_context=_require_mapping(data, "outer_context"),
            declaration_ref=_require_str(data, "declaration_ref"),
            selection_ref=_require_str(data, "selection_ref"),
            resumption_handle_ref=_require_str(data, "resumption_handle_ref"),
            selection_path_ref=_require_str(data, "selection_path_ref"),
            captured_continuation_control_ref=_require_str(data, "captured_continuation_control_ref"),
            outer_continuation_control_ref=_require_str(data, "outer_continuation_control_ref"),
            operation_result_schema_ref=_require_optional_str(data, "operation_result_schema_ref"),
            handled_result_schema_ref=_require_str(data, "handled_result_schema_ref"),
        )
    if frame_kind == "resume-return":
        return ResumeReturnFramePayload(
            resume_ref=_require_str(data, "resume_ref"),
            selection_path_ref=_require_str(data, "selection_path_ref"),
            handler_continuation_stack_ref=_require_str(data, "handler_continuation_stack_ref"),
            handler_return_frame_ref=_require_str(data, "handler_return_frame_ref"),
            handler_dynamic_tail_stack_ref=_require_str(data, "handler_dynamic_tail_stack_ref"),
            handler_context_ref=_require_str(data, "handler_context_ref"),
            handler_context=_require_mapping(data, "handler_context"),
        )
    if frame_kind == "terminal-result":
        return TerminalResultFramePayload(
            resume_ref=_require_str(data, "resume_ref"),
            selection_ref=_require_str(data, "selection_ref"),
            selection_path_ref=_require_str(data, "selection_path_ref"),
            outer_stack_ref=_require_str(data, "outer_stack_ref"),
            delivery_tail_stack_ref=_require_str(data, "delivery_tail_stack_ref"),
            branch_ref=_require_str(data, "branch_ref"),
        )
    raise ContinuationObjectValidationError(f"unknown continuation frame_kind: {frame_kind!r}")


def _frame_summary_from_json(data: Mapping[str, Any]) -> ContinuationFrameSummary:
    return ContinuationFrameSummary(
        required_schema_refs=_require_str_tuple(data, "required_schema_refs"),
        code_identity_refs=_require_str_tuple(data, "code_identity_refs"),
    )


def _stack_summary_from_json(data: Mapping[str, Any]) -> ContinuationStackSummary:
    return ContinuationStackSummary(
        depth=_require_int(data, "depth"),
        required_schema_refs=_require_str_tuple(data, "required_schema_refs"),
        code_identity_refs=_require_str_tuple(data, "code_identity_refs"),
    )


def _concat_stack_summary(
    prefix: ContinuationStackSummary,
    tail: ContinuationStackSummary,
) -> ContinuationStackSummary:
    return ContinuationStackSummary(
        depth=prefix.depth + tail.depth,
    )


def _dataclass_to_payload(value: object) -> dict[str, Any]:
    return {field.name: _payload_value(getattr(value, field.name)) for field in fields(cast("Any", value))}


def _payload_value(value: Any) -> Any:
    if is_dataclass(value) and not isinstance(value, type):
        return _dataclass_to_payload(value)
    if isinstance(value, tuple):
        return tuple(_payload_value(item) for item in value)
    if isinstance(value, list):
        return [_payload_value(item) for item in value]
    if isinstance(value, Mapping):
        return {key: _payload_value(item) for key, item in value.items()}
    return value


def _jsonify(value: Any) -> Any:
    if isinstance(value, tuple):
        return [_jsonify(item) for item in value]
    if isinstance(value, list):
        return [_jsonify(item) for item in value]
    if isinstance(value, Mapping):
        return {key: _jsonify(item) for key, item in value.items()}
    return value


def _freeze_mapping_value(value: Any, *, context: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise TypeError(f"{context} must be a mapping")
    frozen: dict[str, Any] = {}
    for key, item in value.items():
        if not isinstance(key, str):
            raise TypeError(f"{context} contains a non-string mapping key")
        frozen[key] = _freeze_json_compatible(item, context=f"{context}.{key}")
    return _FrozenJsonDict(frozen)


def _freeze_json_compatible(value: Any, *, context: str) -> Any:
    if value is None or isinstance(value, bool | int | str):
        return value
    if isinstance(value, float):
        if not isfinite(value):
            raise TypeError(f"{context} contains a non-finite float")
        return value
    if isinstance(value, tuple | list):
        return tuple(_freeze_json_compatible(item, context=f"{context}[{idx}]") for idx, item in enumerate(value))
    if isinstance(value, Mapping):
        return _freeze_mapping_value(value, context=context)
    raise TypeError(f"{context} contains a non-JSON-compatible value: {value!r}")


def _binding_env_ref(context: Mapping[str, Any]) -> Ref:
    value = context["binding_env_ref"]
    if not isinstance(value, str):
        raise TypeError("context.binding_env_ref must be a string")
    return value


def _sorted_ref_tuple(value: Iterable[Ref]) -> tuple[Ref, ...]:
    refs: list[Ref] = []
    for item in value:
        if not isinstance(item, str):
            raise TypeError(f"continuation ref tuple entries must be strings, got {type(item).__name__}")
        refs.append(item)
    return tuple(sorted(set(refs)))


def _check_object_schema_version(value: str) -> None:
    if value != CONTINUATION_OBJECT_SCHEMA_VERSION:
        raise ValueError(f"object_schema_version must be {CONTINUATION_OBJECT_SCHEMA_VERSION!r}")


def _check_frame_kind(value: str, expected: str) -> None:
    if value != expected:
        raise ValueError(f"frame_kind must be {expected!r}")


def _require_str(data: Mapping[str, Any], key: str) -> str:
    value = data[key]
    if not isinstance(value, str):
        raise TypeError(f"{key} must be a string")
    return value


def _require_ref(value: Ref | None, context: str) -> Ref:
    if value is None:
        raise RuntimeError(f"{context} is required for continuation object projection")
    return value


def _require_optional_str(data: Mapping[str, Any], key: str) -> str | None:
    value = data[key]
    if value is None:
        return None
    if not isinstance(value, str):
        raise TypeError(f"{key} must be a string or null")
    return value


def _require_int(data: Mapping[str, Any], key: str) -> int:
    value = data[key]
    if not isinstance(value, int):
        raise TypeError(f"{key} must be an int")
    return value


def _require_mapping(data: Mapping[str, Any], key: str) -> Mapping[str, Any]:
    value = data[key]
    if not isinstance(value, Mapping):
        raise TypeError(f"{key} must be a mapping")
    return value


def _require_str_tuple(data: Mapping[str, Any], key: str) -> tuple[str, ...]:
    value = data[key]
    if not isinstance(value, tuple | list):
        raise TypeError(f"{key} must be a sequence")
    refs: list[str] = []
    for idx, item in enumerate(value):
        if not isinstance(item, str):
            raise TypeError(f"{key}[{idx}] must be a string")
        refs.append(item)
    return tuple(refs)
