"""Executable defunctionalized abstract machine for the v3 core fragment."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Any, Literal

from shepherd_kernel_v3_reference.kernel.context import ExecutionContext
from shepherd_kernel_v3_reference.kernel.continuation_objects import (
    BindFramePayload,
    ContinuationEmptyStack,
    ContinuationEnvEmpty,
    ContinuationEnvNode,
    ContinuationFrameNode,
    ContinuationKind,
    ContinuationObject,
    ContinuationObjectBuilder,
    ContinuationObjectStore,
    ContinuationRoot,
    ContinuationStackConcat,
    ContinuationStackCursor,
    ContinuationStackNode,
    ContinuationStackSummary,
    HandlerFramePayload,
    HandlerReturnFramePayload,
    ResumeReturnFramePayload,
    continuation_object_ref,
)
from shepherd_kernel_v3_reference.kernel.continuations import (
    ContinuationImage,
    continuation_image_payload,
)
from shepherd_kernel_v3_reference.kernel.events import (
    HandlerCaptured,
    KernelEvent,
    SelectionClosed,
)
from shepherd_kernel_v3_reference.kernel.frame_state import (
    BindFrame,
    Frame,
    HandlerFrame,
    HandlerReturnFrame,
    KontState,
    ResumeReturnFrame,
    _require_ref,
)
from shepherd_kernel_v3_reference.kernel.program_admission import (
    KernelProgramInput,
    PreparedKernelProgram,
    ensure_prepared_kernel_program,
)
from shepherd_kernel_v3_reference.kernel.program_identity import ProgramIdentity, project_program_identity
from shepherd_kernel_v3_reference.kernel.refs import content_ref
from shepherd_kernel_v3_reference.kernel.state import MachineState
from shepherd_kernel_v3_reference.paths import source_path_ref, unhandled_source_path_ref
from shepherd_kernel_v3_reference.schemas import check
from shepherd_kernel_v3_reference.source.effects import EffectRegistry
from shepherd_kernel_v3_reference.source.syntax import Lit, RecordExpr, Var
from shepherd_kernel_v3_reference.source.values import Env

if TYPE_CHECKING:
    from shepherd_kernel_v3_reference.kernel.ir import (
        BinderDef,
        HandlerEnvDef,
        HandlerInstallDef,
        KComputation,
        Ref,
        SchemaDef,
    )


class EvidenceMode(StrEnum):
    """Runtime evidence projection mode."""

    NONE = "none"
    SIDECAR = "sidecar"
    TRACE = "trace"


class EvidenceUnavailableError(RuntimeError):
    """Raised when trace/conformance evidence is requested from execution-only mode."""


@dataclass
class _KernelIdentityStats:
    """Private compute counters for evaluator-local identity caches."""

    program_ref_computes: int = 0
    control_fingerprint_computes: int = 0
    binder_ref_computes: int = 0
    binder_fingerprint_computes: int = 0
    handler_env_ref_computes: int = 0
    handler_env_fingerprint_computes: int = 0
    install_ref_computes: int = 0
    install_fingerprint_computes: int = 0
    schema_fingerprint_computes: int = 0
    schema_ref_fingerprint_computes: int = 0
    kont_state_from_frame_refs_rebuilds: int = 0
    kont_state_from_frame_refs_replayed_frame_refs: int = 0


class _KernelRuntimeServices:
    """Shared runtime state, identity, evidence, and continuation helpers."""

    def __init__(
        self,
        program: KernelProgramInput,
        registry: EffectRegistry | None = None,
        event_sink: Callable[[KernelEvent], None] | None = None,
        *,
        continuation_builder: ContinuationObjectBuilder | None = None,
        evidence_mode: EvidenceMode | str = EvidenceMode.TRACE,
    ):
        prepared = ensure_prepared_kernel_program(program)
        program_snapshot = prepared.program
        self._prepared_program: PreparedKernelProgram = prepared
        self.program = program_snapshot
        self.registry = registry or EffectRegistry()
        self._event_sink = event_sink
        self._state = MachineState(profile=program_snapshot.profile)
        self.evidence_mode = EvidenceMode(evidence_mode)
        self._continuation_builder: ContinuationObjectBuilder | None = (
            continuation_builder if self.evidence_mode is not EvidenceMode.NONE else None
        )
        self._continuation_root_refs: list[Ref] = []
        self._continuation_ref_map: dict[Ref, Ref] = {}
        self._continuation_control_ref_map: dict[Ref, Ref] = {}
        self._continuation_frame_ref_map: dict[Ref, Ref] = {}
        self._context_ref_map: dict[Ref, Ref] = {}
        self._env_ref_map: dict[Ref, Ref] = {}
        self._sidecar_stack_cursor_cache: dict[Ref, ContinuationStackCursor] = {}
        self._continuation_frame_cache: dict[int, tuple[Frame, Ref]] = {}
        self._identity_stats = _KernelIdentityStats()
        self._program_identity: ProgramIdentity | None = None
        self._runtime_next_id = 0
        self._runtime_empty_stack = ContinuationStackCursor(
            stack_ref="stack:runtime:empty",
            summary=ContinuationStackSummary(),
        )
        self._runtime_stack_cursor_cache: dict[tuple[Ref, Ref], ContinuationStackCursor] = {}
        self._runtime_stack_concat_cache: dict[tuple[Ref, Ref], ContinuationStackCursor] = {}
        self._runtime_control_ref_cache: dict[tuple[Ref, Ref, Ref | None], Ref] = {}
        self._runtime_frame_cache: dict[int, tuple[Frame, Ref]] = {}
        self._runtime_env_cache: dict[int, tuple[Env, Ref]] = {}
        self._runtime_context_cache: dict[ExecutionContext, Ref] = {}
        self._evidence_env_cache: dict[int, tuple[Env, Ref]] = {}
        self._replay_env_cache: dict[Ref, Env] = {}

    @property
    def _trace_evidence_enabled(self) -> bool:
        return self.evidence_mode is EvidenceMode.TRACE

    @property
    def _sidecar_evidence_enabled(self) -> bool:
        return self.evidence_mode is EvidenceMode.SIDECAR

    @property
    def _evidence_available(self) -> bool:
        return self.evidence_mode is not EvidenceMode.NONE

    @property
    def _trace_events_enabled(self) -> bool:
        return self._event_sink is not None

    def _evidence_builder(self) -> ContinuationObjectBuilder:
        if not self._evidence_available:
            raise EvidenceUnavailableError("continuation evidence is unavailable in EvidenceMode.NONE")
        if self._continuation_builder is None:
            self._continuation_builder = ContinuationObjectBuilder()
        return self._continuation_builder

    @property
    def program_ref(self) -> Ref:
        if not self._evidence_available:
            raise EvidenceUnavailableError("program_ref is unavailable in EvidenceMode.NONE")
        return self._program_ref()

    @property
    def continuation_images(self) -> Mapping[Ref, ContinuationImage]:
        return dict(self._state.continuation_image_catalog)

    @property
    def continuation_root_refs(self) -> tuple[Ref, ...]:
        if not self._evidence_available:
            raise EvidenceUnavailableError("continuation roots are unavailable in EvidenceMode.NONE")
        return tuple(self._continuation_root_refs)

    @property
    def continuation_objects(self) -> Mapping[Ref, ContinuationObject]:
        if not self._evidence_available:
            raise EvidenceUnavailableError("continuation objects are unavailable in EvidenceMode.NONE")
        builder = self._evidence_builder()
        if not self._continuation_root_refs:
            return {builder.empty_stack_ref: builder.store.get(builder.empty_stack_ref)}
        snapshot: Mapping[Ref, ContinuationObject] = builder.store.snapshot(self._continuation_root_refs)
        return snapshot

    @property
    def continuation_ref_map(self) -> Mapping[Ref, Ref]:
        if not self._evidence_available:
            raise EvidenceUnavailableError("continuation ref map is unavailable in EvidenceMode.NONE")
        return dict(self._continuation_ref_map)

    @property
    def continuation_control_ref_map(self) -> Mapping[Ref, Ref]:
        if not self._evidence_available:
            raise EvidenceUnavailableError("continuation control ref map is unavailable in EvidenceMode.NONE")
        return dict(self._continuation_control_ref_map)

    @property
    def context_ref_map(self) -> Mapping[Ref, Ref]:
        if not self._evidence_available:
            raise EvidenceUnavailableError("context ref map is unavailable in EvidenceMode.NONE")
        return dict(self._context_ref_map)

    @property
    def continuation_object_store(self) -> ContinuationObjectStore:
        return self._evidence_builder().store

    def get_continuation_image(self, ref: Ref) -> ContinuationImage:
        return self._state.continuation_image_catalog[ref]

    def list_continuation_images(self) -> tuple[ContinuationImage, ...]:
        return tuple(self._state.continuation_image_catalog.values())

    def get_continuation_object(self, ref: Ref) -> ContinuationObject:
        return self._evidence_builder().store.get(ref)

    def list_continuation_objects(self) -> tuple[ContinuationObject, ...]:
        return tuple(self.continuation_objects.values())

    def _continuation_object_builder_for_replay(self) -> ContinuationObjectBuilder:
        if self._continuation_builder is None:
            self._continuation_builder = ContinuationObjectBuilder()
        return self._continuation_builder

    def _load_continuation_object_snapshot(self, objects: Mapping[Ref, ContinuationObject]) -> None:
        builder = self._continuation_object_builder_for_replay()
        for ref, obj in objects.items():
            actual_ref = continuation_object_ref(obj)
            if ref != actual_ref:
                raise RuntimeError(f"continuation object ref mismatch for {ref!r}: payload hashes to {actual_ref!r}")
            builder.store.put(obj)

    def _continuation_root_from_objects(
        self,
        root_ref: Ref,
        objects: Mapping[Ref, ContinuationObject],
    ) -> ContinuationRoot:
        self._load_continuation_object_snapshot(objects)
        root = self._continuation_object_for_replay(root_ref)
        if not isinstance(root, ContinuationRoot):
            raise RuntimeError(f"continuation object {root_ref!r} is not a root")
        if root.program_ref != self._program_ref():
            raise RuntimeError("ContinuationRoot program_ref does not match this KernelProgram")
        if root.position != "value":
            raise RuntimeError(f"unsupported ContinuationRoot.position: {root.position!r}")
        context = self._context_from_continuation_payload(
            root.execution_context,
            expected_ref=root.execution_context_ref,
            source="ContinuationRoot.execution_context",
        )
        self._env_from_continuation_object_ref(context.binding_env_ref)
        return root

    def _continuation_object_for_replay(self, ref: Ref) -> ContinuationObject:
        store = self._continuation_object_builder_for_replay().store
        try:
            return store.get(ref)
        except KeyError as exc:
            raise RuntimeError(f"continuation object is missing: {ref!r}") from exc

    def _env_from_continuation_object_ref(self, env_ref: Ref) -> Env:
        cached = self._replay_env_cache.get(env_ref)
        if cached is not None:
            return cached
        obj = self._continuation_object_for_replay(env_ref)
        if isinstance(obj, ContinuationEnvEmpty):
            env = Env()
            self._replay_env_cache[env_ref] = env
            self._evidence_env_cache[id(env)] = (env, env_ref)
            return env
        if isinstance(obj, ContinuationEnvNode):
            parent = self._env_from_continuation_object_ref(obj.parent_env_ref)
            env = parent.extend(obj.name, obj.value)
            if env.depth != obj.depth:
                raise RuntimeError(f"ContinuationEnvNode depth disagrees with parent for {env_ref!r}")
            self._replay_env_cache[env_ref] = env
            self._evidence_env_cache[id(env)] = (env, env_ref)
            return env
        raise RuntimeError(f"continuation object {env_ref!r} is not an env")

    def _kont_state_from_continuation_object_stack_ref(self, stack_ref: Ref) -> KontState:
        obj = self._continuation_object_for_replay(stack_ref)
        if isinstance(obj, ContinuationEmptyStack):
            return KontState.empty(ContinuationStackCursor(stack_ref=stack_ref, summary=obj.summary))
        if isinstance(obj, ContinuationStackNode):
            frame = self._frame_from_continuation_object_ref(obj.head_frame_ref)
            tail = self._kont_state_from_continuation_object_stack_ref(obj.tail_stack_ref)
            cursor = ContinuationStackCursor(stack_ref=stack_ref, summary=obj.summary)
            if cursor.summary.depth != tail.depth + 1:
                raise RuntimeError(f"ContinuationStackNode depth disagrees with tail for {stack_ref!r}")
            return KontState.cons(frame, obj.head_frame_ref, cursor, tail)
        if isinstance(obj, ContinuationStackConcat):
            prefix = self._kont_state_from_continuation_object_stack_ref(obj.prefix_stack_ref)
            tail = self._kont_state_from_continuation_object_stack_ref(obj.tail_stack_ref)
            cursor = ContinuationStackCursor(stack_ref=stack_ref, summary=obj.summary)
            if cursor.summary.depth != prefix.depth + tail.depth:
                raise RuntimeError(f"ContinuationStackConcat depth disagrees with children for {stack_ref!r}")
            result = KontState.concat(prefix, tail, cursor)
            if result.cursor.stack_ref != stack_ref:
                raise RuntimeError(f"ContinuationStackConcat canonical cursor disagrees for {stack_ref!r}")
            return result
        raise RuntimeError(f"continuation object {stack_ref!r} is not a stack")

    def _frame_from_continuation_object_ref(self, frame_ref: Ref) -> Frame:
        obj = self._continuation_object_for_replay(frame_ref)
        if not isinstance(obj, ContinuationFrameNode):
            raise RuntimeError(f"continuation object {frame_ref!r} is not a frame")
        payload = obj.payload
        if isinstance(payload, BindFramePayload):
            return BindFrame(
                binder_id=self._binder_id_by_ref(payload.binder_ref),
                env=self._env_from_continuation_object_ref(payload.env_ref),
                context=self._context_from_continuation_payload(
                    payload.context,
                    expected_ref=payload.context_ref,
                    source="BindFramePayload.context",
                ),
            )
        if isinstance(payload, HandlerFramePayload):
            if payload.handler_env_ref not in self.program.handler_envs:
                raise RuntimeError(f"ContinuationFrame handler env is missing: {payload.handler_env_ref!r}")
            expected_handler_env_ref = self._handler_env_def_ref(payload.handler_env_ref)
            if payload.handler_env_def_ref != expected_handler_env_ref:
                raise RuntimeError("ContinuationFrame handler_env_def_ref does not match this KernelProgram")
            return HandlerFrame(
                handler_env_ref=payload.handler_env_ref,
                env=self._env_from_continuation_object_ref(payload.env_ref),
                region_ref=payload.region_ref,
                entry_context=self._context_from_continuation_payload(
                    payload.entry_context,
                    expected_ref=payload.entry_context_ref,
                    source="HandlerFramePayload.entry_context",
                ),
                outer_context=self._context_from_continuation_payload(
                    payload.outer_context,
                    expected_ref=payload.outer_context_ref,
                    source="HandlerFramePayload.outer_context",
                ),
            )
        if isinstance(payload, HandlerReturnFramePayload):
            install = self._install_by_object_refs(payload.install_ref, payload.install_def_ref)
            selected_handler_frame = self._frame_from_continuation_object_ref(payload.selected_handler_frame_ref)
            if not isinstance(selected_handler_frame, HandlerFrame):
                raise RuntimeError("ContinuationFrame handler-return selected frame is not a handler")
            captured_state = self._kont_state_from_continuation_object_stack_ref(payload.captured_stack_ref)
            outer_state = self._kont_state_from_continuation_object_stack_ref(payload.outer_stack_ref)
            return HandlerReturnFrame(
                install=install,
                selected_handler_frame=selected_handler_frame,
                selected_handler_frame_ref=payload.selected_handler_frame_ref,
                handler_env=self._env_from_continuation_object_ref(payload.handler_binding_env_ref),
                captured_state=captured_state,
                captured_frame_refs=captured_state.frame_refs,
                captured_stack_ref=captured_state.cursor.stack_ref,
                outer_state=outer_state,
                outer_frame_refs=outer_state.frame_refs,
                outer_stack_ref=outer_state.cursor.stack_ref,
                worker_context=self._context_from_continuation_payload(
                    payload.worker_context,
                    expected_ref=payload.worker_context_ref,
                    source="HandlerReturnFramePayload.worker_context",
                ),
                handler_context=self._context_from_continuation_payload(
                    payload.handler_context,
                    expected_ref=payload.handler_context_ref,
                    source="HandlerReturnFramePayload.handler_context",
                ),
                outer_context=self._context_from_continuation_payload(
                    payload.outer_context,
                    expected_ref=payload.outer_context_ref,
                    source="HandlerReturnFramePayload.outer_context",
                ),
                declaration_ref=payload.declaration_ref,
                selection_ref=payload.selection_ref,
                resumption_handle_ref=payload.resumption_handle_ref,
                selection_path_ref=payload.selection_path_ref,
                captured_continuation_control_ref=payload.captured_continuation_control_ref,
                outer_continuation_control_ref=payload.outer_continuation_control_ref,
                operation_result_schema_ref=payload.operation_result_schema_ref,
                handled_result_schema_ref=payload.handled_result_schema_ref,
            )
        if isinstance(payload, ResumeReturnFramePayload):
            handler_return_frame = self._frame_from_continuation_object_ref(payload.handler_return_frame_ref)
            if not isinstance(handler_return_frame, HandlerReturnFrame):
                raise RuntimeError("ContinuationFrame resume-return nested frame is not handler-return")
            handler_continuation = self._kont_state_from_continuation_object_stack_ref(
                payload.handler_continuation_stack_ref
            )
            handler_dynamic_tail = self._kont_state_from_continuation_object_stack_ref(
                payload.handler_dynamic_tail_stack_ref
            )
            return ResumeReturnFrame(
                resume_ref=payload.resume_ref,
                selection_path_ref=payload.selection_path_ref,
                handler_continuation_ref=None,
                handler_dynamic_tail_ref=None,
                handler_continuation_state=handler_continuation,
                handler_continuation_frame_refs=handler_continuation.frame_refs,
                handler_continuation_stack_ref=handler_continuation.cursor.stack_ref,
                handler_return_frame=handler_return_frame,
                handler_return_frame_ref=payload.handler_return_frame_ref,
                handler_dynamic_tail_state=handler_dynamic_tail,
                handler_dynamic_tail_frame_refs=handler_dynamic_tail.frame_refs,
                handler_dynamic_tail_stack_ref=handler_dynamic_tail.cursor.stack_ref,
                handler_context=self._context_from_continuation_payload(
                    payload.handler_context,
                    expected_ref=payload.handler_context_ref,
                    source="ResumeReturnFramePayload.handler_context",
                ),
            )
        raise RuntimeError(f"unsupported continuation frame payload: {payload!r}")

    def _empty_kont_state(self) -> KontState:
        return KontState.empty(self._empty_stack_cursor())

    def _push_kont_frame(self, frame: Frame, tail: KontState) -> KontState:
        frame_ref = self._runtime_frame_ref(frame)
        cursor = self._push_runtime_stack_cursor(frame_ref, tail.cursor)
        self._sidecar_index_pushed_cursor(frame, frame_ref, cursor, tail.cursor)
        return KontState.cons(frame, frame_ref, cursor, tail)

    def _kont_state_from_frame_ref(self, frame: Frame, frame_ref: Ref) -> KontState:
        tail = self._empty_kont_state()
        cursor = self._push_runtime_stack_cursor(frame_ref, tail.cursor)
        self._sidecar_index_pushed_cursor(frame, frame_ref, cursor, tail.cursor)
        return KontState.cons(frame, frame_ref, cursor, tail)

    def _kont_tail(self, kont: KontState) -> KontState:
        return kont.tail_state(self._concat_kont_states)

    def _append_kont_frame(self, prefix: KontState, frame: Frame, frame_ref: Ref) -> KontState:
        return self._concat_kont_states(prefix, self._kont_state_from_frame_ref(frame, frame_ref))

    def _concat_kont_states(self, prefix: KontState, tail: KontState) -> KontState:
        if prefix.is_empty:
            return tail
        if tail.is_empty:
            return prefix
        cursor = self._concat_runtime_stack_cursor(prefix.cursor, tail.cursor)
        if self._sidecar_evidence_enabled:
            self._sidecar_stack_cursor_cache[cursor.stack_ref] = self._evidence_builder().concat_stack(
                self._sidecar_evidence_cursor(prefix.cursor),
                self._sidecar_evidence_cursor(tail.cursor),
            )
        return KontState.concat(prefix, tail, cursor)

    def _sidecar_index_pushed_cursor(
        self,
        frame: Frame,
        frame_ref: Ref,
        cursor: ContinuationStackCursor,
        tail_cursor: ContinuationStackCursor,
    ) -> None:
        if not self._sidecar_evidence_enabled:
            return
        evidence_frame_ref = self._evidence_frame_ref(frame)
        self._continuation_frame_ref_map[frame_ref] = evidence_frame_ref
        self._sidecar_stack_cursor_cache[cursor.stack_ref] = self._evidence_builder().push_frame(
            evidence_frame_ref,
            self._sidecar_evidence_cursor(tail_cursor),
        )

    def _kont_state_from_frame_refs(
        self,
        frames: tuple[Frame, ...],
        frame_refs: tuple[Ref, ...],
        *,
        expected_stack_ref: Ref | None = None,
        tail: KontState | None = None,
    ) -> KontState:
        if len(frames) != len(frame_refs):
            raise RuntimeError("continuation frame/ref tuple lengths disagree")
        state = tail or self._empty_kont_state()
        self._identity_stats.kont_state_from_frame_refs_rebuilds += 1
        self._identity_stats.kont_state_from_frame_refs_replayed_frame_refs += len(frame_refs)
        result = state
        for frame, frame_ref in reversed(tuple(zip(frames, frame_refs, strict=True))):
            cursor = self._push_runtime_stack_cursor(frame_ref, result.cursor)
            self._sidecar_index_pushed_cursor(frame, frame_ref, cursor, result.cursor)
            result = KontState.cons(frame, frame_ref, cursor, result)
        if expected_stack_ref is not None and result.cursor.stack_ref != expected_stack_ref:
            raise RuntimeError("continuation stack ref disagrees with paired frame refs")
        return result

    def _kont_state_from_frames(self, frames: tuple[Frame, ...]) -> KontState:
        """Legacy/debug bridge from by-value frames into paired continuation state."""

        if self._trace_evidence_enabled:
            self._evidence_builder().stats.full_stack_tuple_scans += 1
        frame_refs = tuple(self._runtime_frame_ref(frame) for frame in frames)
        return self._kont_state_from_frame_refs(frames, frame_refs)

    def _handler_return_captured_state(self, frame: HandlerReturnFrame) -> KontState:
        if frame.captured_state is not None:
            return frame.captured_state
        return self._kont_state_from_frame_refs(
            frame.captured_kont,
            frame.captured_frame_refs,
            expected_stack_ref=frame.captured_stack_ref,
        )

    def _handler_return_outer_state(self, frame: HandlerReturnFrame) -> KontState:
        if frame.outer_state is not None:
            return frame.outer_state
        return self._kont_state_from_frame_refs(
            frame.outer_kont,
            frame.outer_frame_refs,
            expected_stack_ref=frame.outer_stack_ref,
        )

    def _resume_handler_continuation_state(self, frame: ResumeReturnFrame) -> KontState:
        if frame.handler_continuation_state is not None:
            return frame.handler_continuation_state
        return self._kont_state_from_frame_refs(
            frame.handler_continuation,
            frame.handler_continuation_frame_refs,
            expected_stack_ref=frame.handler_continuation_stack_ref,
        )

    def _resume_handler_dynamic_tail_state(self, frame: ResumeReturnFrame) -> KontState:
        if frame.handler_dynamic_tail_state is not None:
            return frame.handler_dynamic_tail_state
        return self._kont_state_from_frame_refs(
            frame.handler_dynamic_tail,
            frame.handler_dynamic_tail_frame_refs,
            expected_stack_ref=frame.handler_dynamic_tail_stack_ref,
        )

    def _find_handler(
        self, effect_kind: str, kont: KontState
    ) -> tuple[KontState, HandlerFrame, Ref, KontState, HandlerInstallDef] | None:
        prefix = self._empty_kont_state()
        rest = kont
        while head := rest.head():
            frame, frame_ref = head
            if isinstance(frame, HandlerFrame):
                env_def = self.program.handler_envs[frame.handler_env_ref]
                for install in env_def.bindings:
                    if install.effect_kind == effect_kind:
                        return prefix, frame, frame_ref, self._kont_tail(rest), install
            prefix = self._append_kont_frame(prefix, frame, frame_ref)
            rest = self._kont_tail(rest)
        return None

    def _split_at_handler_return(self, kont: KontState) -> tuple[KontState, HandlerReturnFrame, Ref, KontState] | None:
        prefix = self._empty_kont_state()
        rest = kont
        while head := rest.head():
            frame, frame_ref = head
            if isinstance(frame, HandlerReturnFrame):
                return prefix, frame, frame_ref, self._kont_tail(rest)
            prefix = self._append_kont_frame(prefix, frame, frame_ref)
            rest = self._kont_tail(rest)
        return None

    def _check_schema(self, schema_ref: Ref | None, value: Any, *, context: str) -> None:
        if schema_ref is None:
            return
        schema_def: SchemaDef | None = self.program.schemas.get(schema_ref)
        if schema_def is None:
            raise RuntimeError(f"{context}: kernel schema ref {schema_ref!r} is missing from program.schemas")
        check(schema_def.schema, value, context=context)

    def _fresh_ref(self, prefix: str) -> Ref:
        return self._state.fresh_ref(prefix)

    def _runtime_ref(self, prefix: str) -> Ref:
        ref = f"{prefix}:runtime:{self._runtime_next_id}"
        self._runtime_next_id += 1
        return ref

    def _empty_stack_cursor(self) -> ContinuationStackCursor:
        if self._trace_evidence_enabled:
            return self._evidence_builder().empty_stack
        if self._sidecar_evidence_enabled:
            self._sidecar_stack_cursor_cache[self._runtime_empty_stack.stack_ref] = self._evidence_builder().empty_stack
        return self._runtime_empty_stack

    def _sidecar_evidence_cursor(self, runtime_cursor: ContinuationStackCursor) -> ContinuationStackCursor:
        cached = self._sidecar_stack_cursor_cache.get(runtime_cursor.stack_ref)
        if cached is not None:
            return cached
        if runtime_cursor.summary.depth == 0:
            empty = self._evidence_builder().empty_stack
            self._sidecar_stack_cursor_cache[runtime_cursor.stack_ref] = empty
            return empty
        raise RuntimeError(f"missing sidecar continuation evidence for runtime stack {runtime_cursor.stack_ref!r}")

    def _runtime_frame_ref(self, frame: Frame) -> Ref:
        if self._trace_evidence_enabled:
            return self._evidence_frame_ref(frame)
        cache_key = id(frame)
        cached = self._runtime_frame_cache.get(cache_key)
        if cached is not None:
            cached_frame, cached_ref = cached
            if cached_frame is frame:
                return cached_ref
        ref = self._runtime_ref("frame")
        self._runtime_frame_cache[cache_key] = (frame, ref)
        return ref

    def _push_runtime_stack_cursor(self, frame_ref: Ref, tail: ContinuationStackCursor) -> ContinuationStackCursor:
        if self._trace_evidence_enabled:
            return self._evidence_builder().push_frame(frame_ref, tail)
        cache_key = (frame_ref, tail.stack_ref)
        cached = self._runtime_stack_cursor_cache.get(cache_key)
        if cached is not None:
            return cached
        cursor = ContinuationStackCursor(
            stack_ref=self._runtime_ref("stack"),
            summary=ContinuationStackSummary(depth=tail.summary.depth + 1),
        )
        self._runtime_stack_cursor_cache[cache_key] = cursor
        return cursor

    def _concat_runtime_stack_cursor(
        self,
        prefix: ContinuationStackCursor,
        tail: ContinuationStackCursor,
    ) -> ContinuationStackCursor:
        if prefix.summary.depth == 0:
            return tail
        if tail.summary.depth == 0:
            return prefix
        if self._trace_evidence_enabled:
            return self._evidence_builder().concat_stack(prefix, tail)
        cache_key = (prefix.stack_ref, tail.stack_ref)
        cached = self._runtime_stack_concat_cache.get(cache_key)
        if cached is not None:
            return cached
        cursor = ContinuationStackCursor(
            stack_ref=self._runtime_ref("stack-concat"),
            summary=ContinuationStackSummary(
                depth=prefix.summary.depth + tail.summary.depth,
                required_schema_refs=prefix.summary.required_schema_refs + tail.summary.required_schema_refs,
                code_identity_refs=prefix.summary.code_identity_refs + tail.summary.code_identity_refs,
            ),
        )
        self._runtime_stack_concat_cache[cache_key] = cursor
        return cursor

    def _kont_ref(
        self,
        kont: KontState,
        *,
        continuation_kind: ContinuationKind,
        context: ExecutionContext,
        result_schema_ref: Ref | None = None,
    ) -> Ref:
        if self.evidence_mode is EvidenceMode.NONE:
            return self._runtime_ref("continuation")
        if self._sidecar_evidence_enabled:
            trace_ref = self._runtime_ref("continuation")
            root_ref = self._put_evidence_root(
                self._sidecar_evidence_cursor(kont.cursor),
                continuation_kind=continuation_kind,
                context=context,
                result_schema_ref=result_schema_ref,
            )
            self._continuation_ref_map[trace_ref] = root_ref
            return trace_ref
        return self._put_evidence_root(
            kont.cursor,
            continuation_kind=continuation_kind,
            context=context,
            result_schema_ref=result_schema_ref,
        )

    def _put_evidence_root(
        self,
        stack: ContinuationStackCursor,
        *,
        continuation_kind: ContinuationKind,
        context: ExecutionContext,
        result_schema_ref: Ref | None = None,
    ) -> Ref:
        builder = self._evidence_builder()
        root_ref = builder.put_root(
            stack,
            program_ref=self._program_ref(),
            branch_ref=self._state.branch_ref,
            branch_scope_ref=self._state.branch_scope_ref,
            continuation_kind=continuation_kind,
            execution_context_ref=self._evidence_context_ref(context),
            execution_context=self._evidence_context_payload(context),
            result_schema_ref=result_schema_ref,
        )
        self._continuation_root_refs.append(root_ref)
        return root_ref

    def _kont_control_ref(self, kont: KontState) -> Ref:
        if self.evidence_mode is EvidenceMode.NONE:
            return self._runtime_kont_control_ref(kont)
        if self._sidecar_evidence_enabled:
            trace_ref = self._runtime_kont_control_ref(kont)
            evidence_ref = self._put_evidence_control_identity(self._sidecar_evidence_cursor(kont.cursor))
            self._continuation_control_ref_map[trace_ref] = evidence_ref
            return trace_ref
        return self._put_evidence_control_identity(kont.cursor)

    def _runtime_kont_control_ref(self, kont: KontState) -> Ref:
        key = (kont.cursor.stack_ref, self._state.branch_ref, self._state.branch_scope_ref)
        cached = self._runtime_control_ref_cache.get(key)
        if cached is not None:
            return cached
        ref = self._runtime_ref("continuation-control")
        self._runtime_control_ref_cache[key] = ref
        return ref

    def _put_evidence_control_identity(self, stack: ContinuationStackCursor) -> Ref:
        return self._evidence_builder().put_control_identity(
            stack,
            program_ref=self._program_ref(),
            branch_ref=self._state.branch_ref,
            branch_scope_ref=self._state.branch_scope_ref,
        )

    def _continuation_frame_ref(self, frame: Frame) -> Ref:
        if not self._trace_evidence_enabled:
            return self._runtime_frame_ref(frame)
        return self._evidence_frame_ref(frame)

    def _evidence_frame_ref(self, frame: Frame) -> Ref:
        cache_key = id(frame)
        cached = self._continuation_frame_cache.get(cache_key)
        if cached is not None:
            cached_frame, cached_ref = cached
            if cached_frame is frame:
                return cached_ref
        ref = self._evidence_builder().put_frame(self._continuation_frame_payload(frame))
        self._continuation_frame_cache[cache_key] = (frame, ref)
        return ref

    def _continuation_frame_payload(
        self,
        frame: Frame,
    ) -> BindFramePayload | HandlerFramePayload | HandlerReturnFramePayload | ResumeReturnFramePayload:
        if isinstance(frame, BindFrame):
            return BindFramePayload(
                binder_ref=self._binder_ref(frame.binder_id),
                env_ref=self._evidence_env_ref(frame.env),
                context_ref=self._evidence_context_ref(frame.context),
                context=self._evidence_context_payload(frame.context),
            )
        if isinstance(frame, HandlerFrame):
            return HandlerFramePayload(
                handler_env_ref=frame.handler_env_ref,
                handler_env_def_ref=self._handler_env_def_ref(frame.handler_env_ref),
                region_ref=frame.region_ref,
                env_ref=self._evidence_env_ref(frame.env),
                entry_context_ref=self._evidence_context_ref(frame.entry_context),
                entry_context=self._evidence_context_payload(frame.entry_context),
                outer_context_ref=self._evidence_context_ref(frame.outer_context),
                outer_context=self._evidence_context_payload(frame.outer_context),
            )
        if isinstance(frame, HandlerReturnFrame):
            captured_state = self._handler_return_captured_state(frame)
            outer_state = self._handler_return_outer_state(frame)
            return HandlerReturnFramePayload(
                captured_stack_ref=self._evidence_stack_ref(captured_state),
                selected_handler_frame_ref=self._evidence_frame_ref_for_trace(
                    _require_ref(
                        frame.selected_handler_frame_ref,
                        "HandlerReturnFrame.selected_handler_frame_ref",
                    )
                ),
                outer_stack_ref=self._evidence_stack_ref(outer_state),
                install_ref=frame.install.install_ref,
                install_def_ref=self._install_ref(frame.install),
                declaration_ref=_require_ref(frame.declaration_ref, "HandlerReturnFrame.declaration_ref"),
                selection_ref=_require_ref(frame.selection_ref, "HandlerReturnFrame.selection_ref"),
                resumption_handle_ref=_require_ref(
                    frame.resumption_handle_ref,
                    "HandlerReturnFrame.resumption_handle_ref",
                ),
                selection_path_ref=_require_ref(frame.selection_path_ref, "HandlerReturnFrame.selection_path_ref"),
                captured_continuation_control_ref=(
                    self._evidence_control_ref_for_trace(frame.captured_continuation_control_ref)
                    if frame.captured_continuation_control_ref is not None
                    else self._evidence_control_ref_for_trace(self._kont_control_ref(captured_state))
                ),
                outer_continuation_control_ref=(
                    self._evidence_control_ref_for_trace(frame.outer_continuation_control_ref)
                    if frame.outer_continuation_control_ref is not None
                    else self._evidence_control_ref_for_trace(self._kont_control_ref(outer_state))
                ),
                handler_binding_env_ref=self._evidence_env_ref(frame.handler_env),
                worker_context_ref=self._evidence_context_ref(frame.worker_context),
                worker_context=self._evidence_context_payload(frame.worker_context),
                handler_context_ref=self._evidence_context_ref(frame.handler_context),
                handler_context=self._evidence_context_payload(frame.handler_context),
                outer_context_ref=self._evidence_context_ref(frame.outer_context),
                outer_context=self._evidence_context_payload(frame.outer_context),
                operation_result_schema_ref=frame.operation_result_schema_ref,
                handled_result_schema_ref=_require_ref(
                    frame.handled_result_schema_ref,
                    "HandlerReturnFrame.handled_result_schema_ref",
                ),
            )
        if isinstance(frame, ResumeReturnFrame):
            handler_continuation = self._resume_handler_continuation_state(frame)
            handler_dynamic_tail = self._resume_handler_dynamic_tail_state(frame)
            return ResumeReturnFramePayload(
                resume_ref=_require_ref(frame.resume_ref, "ResumeReturnFrame.resume_ref"),
                selection_path_ref=_require_ref(frame.selection_path_ref, "ResumeReturnFrame.selection_path_ref"),
                handler_continuation_stack_ref=self._evidence_stack_ref(handler_continuation),
                handler_return_frame_ref=self._evidence_frame_ref_for_trace(
                    _require_ref(
                        frame.handler_return_frame_ref,
                        "ResumeReturnFrame.handler_return_frame_ref",
                    )
                ),
                handler_dynamic_tail_stack_ref=self._evidence_stack_ref(handler_dynamic_tail),
                handler_context_ref=self._evidence_context_ref(frame.handler_context),
                handler_context=self._evidence_context_payload(frame.handler_context),
            )
        raise TypeError(f"unknown continuation frame: {frame!r}")

    def _evidence_stack_ref(self, state: KontState) -> Ref:
        if self._sidecar_evidence_enabled:
            return self._sidecar_evidence_cursor(state.cursor).stack_ref
        return state.cursor.stack_ref

    def _evidence_frame_ref_for_trace(self, trace_ref: Ref) -> Ref:
        if self._sidecar_evidence_enabled:
            return self._continuation_frame_ref_map.get(trace_ref, trace_ref)
        return trace_ref

    def _evidence_control_ref_for_trace(self, trace_ref: Ref) -> Ref:
        if self._sidecar_evidence_enabled:
            return self._continuation_control_ref_map.get(trace_ref, trace_ref)
        return trace_ref

    def _register_continuation_image(self, image: ContinuationImage) -> Ref:
        ref = image.ref
        if ref is None:
            raise RuntimeError("ContinuationImage is missing its content-addressed ref")
        existing = self._state.continuation_image_catalog.get(ref)
        if existing is None:
            self._state.continuation_image_catalog[ref] = image
            return ref
        if continuation_image_payload(existing) != continuation_image_payload(image):
            raise RuntimeError(f"ContinuationImage catalog collision for content-addressed ref {ref!r}")
        return ref

    def _source_path_ref(self, selection_ref: Ref | None, source_ref: Ref) -> Ref:
        if selection_ref is None:
            return unhandled_source_path_ref(source_ref, self._state.branch_ref)
        return source_path_ref(selection_ref, source_ref, self._state.branch_ref)

    def _source_path_ref_for_branch(self, selection_ref: Ref | None, source_ref: Ref, branch_ref: Ref) -> Ref:
        if selection_ref is None:
            return unhandled_source_path_ref(source_ref, branch_ref)
        return source_path_ref(selection_ref, source_ref, branch_ref)

    def _env_ref(self, env: Env) -> Ref:
        if self._trace_evidence_enabled:
            return self._evidence_env_ref(env)
        runtime_ref = self._runtime_env_ref(env)
        if self._sidecar_evidence_enabled:
            self._env_ref_map[runtime_ref] = self._evidence_env_ref(env)
        return runtime_ref

    def _evidence_env_ref(self, env: Env) -> Ref:
        cache_key = id(env)
        cached = self._evidence_env_cache.get(cache_key)
        if cached is not None:
            cached_env, cached_ref = cached
            if cached_env is env:
                return cached_ref

        builder = self._evidence_builder()
        pending: list[Env] = []
        current: Env | None = env
        parent_ref: Ref | None = None
        while current is not None:
            cached = self._evidence_env_cache.get(id(current))
            if cached is not None:
                cached_env, cached_ref = cached
                if cached_env is current:
                    parent_ref = cached_ref
                    break
            parent, _name, _value, depth = current.node_parts()
            if parent is None:
                parent_ref = builder.empty_env_ref
                self._evidence_env_cache[id(current)] = (current, parent_ref)
                break
            pending.append(current)
            current = parent

        if parent_ref is None:
            raise RuntimeError("env evidence projection failed to locate an env root")

        for node in reversed(pending):
            parent, name, value, depth = node.node_parts()
            if parent is None or name is None:
                raise RuntimeError("malformed env node")
            parent_ref = builder.put_env_node(
                parent_env_ref=parent_ref,
                name=name,
                value=value,
                depth=depth,
            )
            self._evidence_env_cache[id(node)] = (node, parent_ref)

        return parent_ref

    def _runtime_env_ref(self, env: Env) -> Ref:
        cache_key = id(env)
        cached = self._runtime_env_cache.get(cache_key)
        if cached is not None:
            cached_env, cached_ref = cached
            if cached_env is env:
                return cached_ref
        ref = self._runtime_ref("env")
        self._runtime_env_cache[cache_key] = (env, ref)
        return ref

    def _context_ref(self, context: ExecutionContext) -> Ref:
        if self._trace_evidence_enabled:
            return self._evidence_context_ref(context)
        cached = self._runtime_context_cache.get(context)
        if cached is not None:
            return cached
        ref = self._runtime_ref("ctx")
        self._runtime_context_cache[context] = ref
        if self._sidecar_evidence_enabled:
            self._context_ref_map[ref] = self._evidence_context_ref(context)
        return ref

    def _evidence_context_ref(self, context: ExecutionContext) -> Ref:
        return content_ref("ctx", self._evidence_context_payload(context))

    def _context_payload(self, context: ExecutionContext) -> Mapping[str, Ref]:
        return {
            "binding_env_ref": self._context_binding_env_ref(context),
            "region_ref": context.region_ref,
            "authority_ref": context.authority_ref,
        }

    def _evidence_context_payload(self, context: ExecutionContext) -> Mapping[str, Ref]:
        return {
            "binding_env_ref": self._evidence_context_binding_env_ref(context),
            "region_ref": context.region_ref,
            "authority_ref": context.authority_ref,
        }

    def _context_binding_env_ref(self, context: ExecutionContext) -> Ref:
        if self._trace_evidence_enabled and context.binding_env_ref == "env:root":
            return self._evidence_builder().empty_env_ref
        return context.binding_env_ref

    def _evidence_context_binding_env_ref(self, context: ExecutionContext) -> Ref:
        if context.binding_env_ref == "env:root":
            return self._evidence_builder().empty_env_ref
        return self._env_ref_map.get(context.binding_env_ref, context.binding_env_ref)

    def _program_ref(self) -> Ref:
        return self._ensure_program_identity().program_ref

    def _ensure_program_identity(self) -> ProgramIdentity:
        if self._program_identity is not None:
            return self._program_identity
        was_prepared_identity_cached = getattr(self._prepared_program, "_identity_cache", None) is not None
        identity = project_program_identity(self._prepared_program)
        self._program_identity = identity
        if not was_prepared_identity_cached:
            self._identity_stats.program_ref_computes += 1
            self._identity_stats.control_fingerprint_computes += len(identity.control_fingerprints)
            self._identity_stats.binder_ref_computes += len(identity.binder_refs)
            self._identity_stats.binder_fingerprint_computes += len(identity.binder_fingerprints)
            self._identity_stats.handler_env_ref_computes += len(identity.handler_env_refs)
            self._identity_stats.handler_env_fingerprint_computes += len(identity.handler_env_fingerprints)
            self._identity_stats.install_ref_computes += len(identity.install_refs_by_node)
            self._identity_stats.install_fingerprint_computes += len(identity.install_fingerprints_by_node)
            self._identity_stats.schema_fingerprint_computes += 1
            self._identity_stats.schema_ref_fingerprint_computes += len(identity.schema_ref_fingerprints)
        return identity

    def _binder_ref(self, binder_id: Ref) -> Ref:
        return self._ensure_program_identity().binder_refs[binder_id]

    def _handler_env_def_ref(self, handler_env_ref: Ref) -> Ref:
        return self._ensure_program_identity().handler_env_refs[handler_env_ref]

    def _install_ref(self, install: HandlerInstallDef) -> Ref:
        return self._ensure_program_identity().install_refs_by_object_id[id(install)]

    def _emit(self, event: KernelEvent) -> None:
        if self._event_sink is not None:
            self._event_sink(event)

    def _emit_capture(
        self,
        frame: HandlerReturnFrame,
        *,
        action_kind: Literal["return", "abort"],
        action_payload: Any,
        continuation_disposition: Literal["completed", "aborted"],
    ) -> Ref | None:
        if frame.selection_ref is None or frame.selection_path_ref is None:
            return None
        if frame.selection_path_ref in self._state.terminal_paths:
            raise RuntimeError(f"selection path already terminal: {frame.selection_path_ref}")
        self._state.mark_terminal_path(frame.selection_path_ref)
        capture_ref = self._fresh_ref("capture")
        if self._trace_events_enabled:
            self._emit(
                HandlerCaptured(
                    ref=capture_ref,
                    selection_ref=frame.selection_ref,
                    selection_path_ref=frame.selection_path_ref,
                    branch_ref=self._state.branch_ref,
                    action_kind=action_kind,
                    action_payload=action_payload,
                    continuation_disposition=continuation_disposition,
                    outer_context_ref=self._context_ref(frame.outer_context),
                    branch_scope_ref=self._state.branch_scope_ref,
                )
            )
        return capture_ref

    def _close_abandoned_selections(
        self,
        kont: KontState | tuple[Frame, ...],
        *,
        reason: Literal["skipped_by_outer_abort", "abandoned", "runtime_failure"],
        caused_by_ref: Ref | None,
        caused_by_record_type: Literal["EffectCapture", "RuntimeFailure"],
        closed_by_selection_ref: Ref | None,
        closed_by_selection_path_ref: Ref | None,
    ) -> None:
        if caused_by_ref is None or closed_by_selection_ref is None or closed_by_selection_path_ref is None:
            return
        for frame in self._abandoned_handler_returns(kont, seen=set()):
            if frame.selection_ref is None or frame.selection_path_ref is None:
                continue
            if frame.selection_path_ref in self._state.terminal_paths:
                continue
            self._state.mark_terminal_path(frame.selection_path_ref)
            selection_closed_ref = self._fresh_ref("selection-closed")
            if self._trace_events_enabled:
                self._emit(
                    SelectionClosed(
                        ref=selection_closed_ref,
                        selection_ref=frame.selection_ref,
                        selection_path_ref=frame.selection_path_ref,
                        branch_ref=self._state.branch_ref,
                        reason=reason,
                        caused_by_ref=caused_by_ref,
                        caused_by_record_type=caused_by_record_type,
                        closed_by_selection_ref=closed_by_selection_ref,
                        closed_by_selection_path_ref=closed_by_selection_path_ref,
                        branch_scope_ref=self._state.branch_scope_ref,
                    )
                )

    def _abandoned_handler_returns(
        self,
        kont: KontState | tuple[Frame, ...],
        *,
        seen: set[int],
    ) -> tuple[HandlerReturnFrame, ...]:
        frames: list[HandlerReturnFrame] = []
        kont_frames = tuple(kont.iter_frames()) if isinstance(kont, KontState) else kont
        pending = list(reversed(kont_frames))
        while pending:
            frame = pending.pop()
            frame_id = id(frame)
            if frame_id in seen:
                continue
            seen.add(frame_id)
            if isinstance(frame, HandlerReturnFrame):
                frames.append(frame)
                pending.extend(reversed(tuple(self._handler_return_outer_state(frame).iter_frames())))
                pending.extend(reversed(tuple(self._handler_return_captured_state(frame).iter_frames())))
                continue
            if isinstance(frame, ResumeReturnFrame):
                frames.append(frame.handler_return_frame)
                pending.extend(reversed(tuple(self._resume_handler_dynamic_tail_state(frame).iter_frames())))
                pending.extend(reversed(tuple(self._resume_handler_continuation_state(frame).iter_frames())))
        return tuple(frames)

    def _schema_refs_from_frames(self, frames: tuple[object, ...]) -> tuple[Ref, ...]:
        refs: set[Ref] = set()
        self._collect_refs_by_key_suffix(frames, suffix="_schema_ref", refs=refs)
        return tuple(sorted(refs))

    def _code_identity_refs_from_frames(
        self,
        frames: tuple[object, ...],
        *,
        program_ref: Ref,
    ) -> tuple[Ref, ...]:
        refs: set[Ref] = {program_ref}
        self._collect_refs_by_key(
            frames,
            keys={"binder_ref", "handler_env_def_ref", "install_def_ref"},
            refs=refs,
        )
        return tuple(sorted(refs))

    def _collect_refs_by_key_suffix(
        self,
        value: object,
        *,
        suffix: str,
        refs: set[Ref],
    ) -> None:
        if isinstance(value, Mapping):
            for key, item in value.items():
                if isinstance(key, str) and key.endswith(suffix) and isinstance(item, str):
                    refs.add(item)
                self._collect_refs_by_key_suffix(item, suffix=suffix, refs=refs)
            return
        if isinstance(value, tuple | list):
            for item in value:
                self._collect_refs_by_key_suffix(item, suffix=suffix, refs=refs)

    def _collect_refs_by_key(
        self,
        value: object,
        *,
        keys: set[str],
        refs: set[Ref],
    ) -> None:
        if isinstance(value, Mapping):
            for key, item in value.items():
                if key in keys and isinstance(item, str):
                    refs.add(item)
                self._collect_refs_by_key(item, keys=keys, refs=refs)
            return
        if isinstance(value, tuple | list):
            for item in value:
                self._collect_refs_by_key(item, keys=keys, refs=refs)

    def _kont_from_image(self, frames: tuple[Mapping[str, Any], ...]) -> tuple[Frame, ...]:
        return tuple(self._frame_from_image(frame) for frame in frames)

    def _frame_from_image(self, frame: Mapping[str, Any]) -> Frame:
        frame_kind = self._require_str(frame, "frame")
        if frame_kind == "bind":
            binder_id = self._require_str(frame, "binder_id")
            if binder_id not in self.program.binders:
                raise RuntimeError(f"ContinuationImage binder is missing: {binder_id!r}")
            return BindFrame(
                binder_id=binder_id,
                env=self._env_from_payload(frame["env"]),
                context=self._context_from_payload(frame["context"]),
            )
        if frame_kind == "handler":
            handler_env_ref = self._require_str(frame, "handler_env_ref")
            if handler_env_ref not in self.program.handler_envs:
                raise RuntimeError(f"ContinuationImage handler env is missing: {handler_env_ref!r}")
            return HandlerFrame(
                handler_env_ref=handler_env_ref,
                env=self._env_from_payload(frame["env"]),
                region_ref=self._require_str(frame, "region_ref"),
                entry_context=self._context_from_payload(frame["entry_context"]),
                outer_context=self._context_from_payload(frame["outer_context"]),
            )
        if frame_kind == "handler-return":
            install_ref = self._require_str(frame, "install_ref")
            install = self._install_by_ref(install_ref)
            selected_handler_frame = self._frame_from_image(self._require_mapping(frame, "selected_handler_frame"))
            if not isinstance(selected_handler_frame, HandlerFrame):
                raise RuntimeError("ContinuationImage handler-return selected frame is not a handler")
            captured_kont = self._kont_from_image(self._require_mapping_tuple(frame, "captured_kont"))
            captured_state = self._kont_state_from_frames(captured_kont)
            selected_handler_frame_ref = self._continuation_frame_ref(selected_handler_frame)
            outer_kont = self._kont_from_image(self._require_mapping_tuple(frame, "outer_kont"))
            outer_state = self._kont_state_from_frames(outer_kont)
            return HandlerReturnFrame(
                install=install,
                captured_kont=captured_kont,
                captured_frame_refs=captured_state.frame_refs,
                captured_stack_ref=captured_state.cursor.stack_ref,
                selected_handler_frame=selected_handler_frame,
                selected_handler_frame_ref=selected_handler_frame_ref,
                outer_kont=outer_kont,
                outer_frame_refs=outer_state.frame_refs,
                outer_stack_ref=outer_state.cursor.stack_ref,
                handler_env=self._env_from_payload(frame["handler_env"]),
                worker_context=self._context_from_payload(frame["worker_context"]),
                handler_context=self._context_from_payload(frame["handler_context"]),
                outer_context=self._context_from_payload(frame["outer_context"]),
                declaration_ref=self._optional_str(frame, "declaration_ref"),
                selection_ref=self._optional_str(frame, "selection_ref"),
                resumption_handle_ref=self._optional_str(frame, "resumption_handle_ref"),
                selection_path_ref=self._optional_str(frame, "selection_path_ref"),
                captured_continuation_ref=self._optional_str(
                    frame,
                    "captured_continuation_ref",
                ),
                outer_continuation_ref=self._optional_str(
                    frame,
                    "outer_continuation_ref",
                ),
                captured_continuation_control_ref=self._optional_str(
                    frame,
                    "captured_continuation_control_ref",
                ),
                outer_continuation_control_ref=self._optional_str(
                    frame,
                    "outer_continuation_control_ref",
                ),
                operation_result_schema_ref=self._optional_str(
                    frame,
                    "operation_result_schema_ref",
                ),
                handled_result_schema_ref=self._optional_str(
                    frame,
                    "handled_result_schema_ref",
                ),
            )
        if frame_kind == "resume-return":
            handler_return_frame = self._frame_from_image(self._require_mapping(frame, "handler_return_frame"))
            if not isinstance(handler_return_frame, HandlerReturnFrame):
                raise RuntimeError("ContinuationImage resume-return nested frame is not handler-return")
            handler_continuation = self._kont_from_image(self._require_mapping_tuple(frame, "handler_continuation"))
            handler_continuation_state = self._kont_state_from_frames(handler_continuation)
            handler_return_frame_ref = self._continuation_frame_ref(handler_return_frame)
            handler_dynamic_tail = self._kont_from_image(self._require_mapping_tuple(frame, "handler_dynamic_tail"))
            handler_dynamic_tail_state = self._kont_state_from_frames(handler_dynamic_tail)
            return ResumeReturnFrame(
                resume_ref=self._optional_str(frame, "resume_ref"),
                selection_path_ref=self._optional_str(frame, "selection_path_ref"),
                handler_continuation_ref=self._optional_str(
                    frame,
                    "handler_continuation_ref",
                ),
                handler_dynamic_tail_ref=self._optional_str(
                    frame,
                    "handler_dynamic_tail_ref",
                ),
                handler_continuation=handler_continuation,
                handler_continuation_frame_refs=handler_continuation_state.frame_refs,
                handler_continuation_stack_ref=handler_continuation_state.cursor.stack_ref,
                handler_return_frame=handler_return_frame,
                handler_return_frame_ref=handler_return_frame_ref,
                handler_dynamic_tail=handler_dynamic_tail,
                handler_dynamic_tail_frame_refs=handler_dynamic_tail_state.frame_refs,
                handler_dynamic_tail_stack_ref=handler_dynamic_tail_state.cursor.stack_ref,
                handler_context=self._context_from_payload(frame["handler_context"]),
            )
        raise RuntimeError(f"unknown ContinuationImage frame: {frame_kind!r}")

    def _binder_id_by_ref(self, binder_ref: Ref) -> Ref:
        matches = sorted(
            binder_id
            for binder_id, projected_ref in self._ensure_program_identity().binder_refs.items()
            if projected_ref == binder_ref
        )
        if not matches:
            raise RuntimeError(f"ContinuationFrame binder is missing: {binder_ref!r}")
        return matches[0]

    def _install_by_object_refs(self, install_ref: Ref, install_def_ref: Ref) -> HandlerInstallDef:
        for handler_env in self.program.handler_envs.values():
            for install in handler_env.bindings:
                if install.install_ref == install_ref:
                    if self._install_ref(install) != install_def_ref:
                        raise RuntimeError("ContinuationFrame install_def_ref does not match this KernelProgram")
                    return install
        raise RuntimeError(f"ContinuationFrame install is missing: {install_ref!r}")

    def _install_by_ref(self, install_ref: Ref) -> HandlerInstallDef:
        for handler_env in self.program.handler_envs.values():
            for install in handler_env.bindings:
                if install.install_ref == install_ref:
                    return install
        raise RuntimeError(f"ContinuationImage install is missing: {install_ref!r}")

    def _context_from_continuation_payload(
        self,
        value: Mapping[str, Any],
        *,
        expected_ref: Ref,
        source: str,
    ) -> ExecutionContext:
        payload = self._require_mapping_value(value, source)
        actual_ref = content_ref("ctx", payload)
        if actual_ref != expected_ref:
            raise RuntimeError(f"{source}_ref does not match payload")
        context = ExecutionContext(
            binding_env_ref=self._require_str(payload, "binding_env_ref"),
            region_ref=self._require_str(payload, "region_ref"),
            authority_ref=self._require_str(payload, "authority_ref"),
        )
        self._env_from_continuation_object_ref(context.binding_env_ref)
        return context

    def _env_from_payload(self, value: object) -> Env:
        if not isinstance(value, tuple | list):
            raise RuntimeError("ContinuationImage env must be a sequence")
        bindings: list[tuple[str, Any]] = []
        for item in value:
            if not isinstance(item, tuple | list) or len(item) != 2:
                raise RuntimeError("ContinuationImage env binding must be a pair")
            name, bound_value = item
            if not isinstance(name, str):
                raise RuntimeError("ContinuationImage env binding name must be a string")
            bindings.append((name, bound_value))
        return Env(tuple(bindings))

    def _context_from_payload(self, value: object) -> ExecutionContext:
        context = self._require_mapping_value(value, "ContinuationImage context")
        return ExecutionContext(
            binding_env_ref=self._require_str(context, "binding_env_ref"),
            region_ref=self._require_str(context, "region_ref"),
            authority_ref=self._require_str(context, "authority_ref"),
        )

    def _require_mapping(
        self,
        value: Mapping[str, Any],
        key: str,
    ) -> Mapping[str, Any]:
        return self._require_mapping_value(value[key], f"ContinuationImage.{key}")

    def _require_mapping_tuple(
        self,
        value: Mapping[str, Any],
        key: str,
    ) -> tuple[Mapping[str, Any], ...]:
        items = value[key]
        if not isinstance(items, tuple | list):
            raise RuntimeError(f"ContinuationImage.{key} must be a sequence")
        return tuple(self._require_mapping_value(item, f"ContinuationImage.{key}[]") for item in items)

    def _require_mapping_value(self, value: object, context: str) -> Mapping[str, Any]:
        if not isinstance(value, Mapping):
            raise RuntimeError(f"{context} must be a mapping")
        return value

    def _require_str(self, value: Mapping[str, Any], key: str) -> str:
        item = value[key]
        if not isinstance(item, str):
            raise RuntimeError(f"ContinuationImage.{key} must be a string")
        return item

    def _optional_str(self, value: Mapping[str, Any], key: str) -> str | None:
        item = value[key]
        if item is None:
            return None
        if not isinstance(item, str):
            raise RuntimeError(f"ContinuationImage.{key} must be a string or null")
        return item

    def _kont_fingerprint(self, kont: KontState | tuple[Frame, ...]) -> tuple[object, ...]:
        frames = kont.iter_frames() if isinstance(kont, KontState) else iter(kont)
        return tuple(self._frame_fingerprint(frame) for frame in frames)

    def _kont_control_fingerprint(self, kont: KontState | tuple[Frame, ...]) -> tuple[object, ...]:
        frames = kont.iter_frames() if isinstance(kont, KontState) else iter(kont)
        return tuple(self._frame_control_fingerprint(frame) for frame in frames)

    def _frame_fingerprint(self, frame: Frame) -> object:
        if isinstance(frame, BindFrame):
            return {
                "frame": "bind",
                "binder_id": frame.binder_id,
                "binder_ref": self._binder_ref(frame.binder_id),
                "env": frame.env.bindings,
                "context_ref": self._context_ref(frame.context),
                "context": self._context_payload(frame.context),
            }
        if isinstance(frame, HandlerFrame):
            return {
                "frame": "handler",
                "handler_env_ref": frame.handler_env_ref,
                "handler_env_def_ref": self._handler_env_def_ref(frame.handler_env_ref),
                "region_ref": frame.region_ref,
                "env": frame.env.bindings,
                "entry_context_ref": self._context_ref(frame.entry_context),
                "entry_context": self._context_payload(frame.entry_context),
                "outer_context_ref": self._context_ref(frame.outer_context),
                "outer_context": self._context_payload(frame.outer_context),
            }
        if isinstance(frame, HandlerReturnFrame):
            return {
                "frame": "handler-return",
                "install_ref": frame.install.install_ref,
                "install_def_ref": self._install_ref(frame.install),
                "captured_kont": self._kont_fingerprint(self._handler_return_captured_state(frame)),
                "selected_handler_frame": self._frame_fingerprint(frame.selected_handler_frame),
                "outer_kont": self._kont_fingerprint(self._handler_return_outer_state(frame)),
                "handler_env": frame.handler_env.bindings,
                "worker_context_ref": self._context_ref(frame.worker_context),
                "worker_context": self._context_payload(frame.worker_context),
                "handler_context_ref": self._context_ref(frame.handler_context),
                "handler_context": self._context_payload(frame.handler_context),
                "outer_context_ref": self._context_ref(frame.outer_context),
                "outer_context": self._context_payload(frame.outer_context),
                "declaration_ref": frame.declaration_ref,
                "selection_ref": frame.selection_ref,
                "resumption_handle_ref": frame.resumption_handle_ref,
                "selection_path_ref": frame.selection_path_ref,
                "captured_continuation_ref": frame.captured_continuation_ref,
                "outer_continuation_ref": frame.outer_continuation_ref,
                "captured_continuation_control_ref": (frame.captured_continuation_control_ref),
                "outer_continuation_control_ref": frame.outer_continuation_control_ref,
                "operation_result_schema_ref": frame.operation_result_schema_ref,
                "handled_result_schema_ref": frame.handled_result_schema_ref,
            }
        if isinstance(frame, ResumeReturnFrame):
            return {
                "frame": "resume-return",
                "resume_ref": frame.resume_ref,
                "selection_path_ref": frame.selection_path_ref,
                "handler_continuation_ref": frame.handler_continuation_ref,
                "handler_dynamic_tail_ref": frame.handler_dynamic_tail_ref,
                "handler_continuation": self._kont_fingerprint(self._resume_handler_continuation_state(frame)),
                "handler_return_frame": self._frame_fingerprint(frame.handler_return_frame),
                "handler_dynamic_tail": self._kont_fingerprint(self._resume_handler_dynamic_tail_state(frame)),
                "handler_context_ref": self._context_ref(frame.handler_context),
                "handler_context": self._context_payload(frame.handler_context),
            }
        raise TypeError(f"unknown continuation frame: {frame!r}")

    def _frame_control_fingerprint(self, frame: Frame) -> object:
        if isinstance(frame, BindFrame):
            return {
                "frame": "bind",
                "binder_id": frame.binder_id,
                "binder_ref": self._binder_ref(frame.binder_id),
                "env": frame.env.bindings,
                "context_ref": self._context_ref(frame.context),
                "context": self._context_payload(frame.context),
            }
        if isinstance(frame, HandlerFrame):
            return {
                "frame": "handler",
                "handler_env_ref": frame.handler_env_ref,
                "handler_env_def_ref": self._handler_env_def_ref(frame.handler_env_ref),
                "region_ref": frame.region_ref,
                "env": frame.env.bindings,
                "entry_context_ref": self._context_ref(frame.entry_context),
                "entry_context": self._context_payload(frame.entry_context),
                "outer_context_ref": self._context_ref(frame.outer_context),
                "outer_context": self._context_payload(frame.outer_context),
            }
        if isinstance(frame, HandlerReturnFrame):
            captured_state = self._handler_return_captured_state(frame)
            outer_state = self._handler_return_outer_state(frame)
            captured_control_ref = self._kont_control_ref(captured_state)
            outer_control_ref = self._kont_control_ref(outer_state)
            return {
                "frame": "handler-return",
                "install_ref": frame.install.install_ref,
                "install_def_ref": self._install_ref(frame.install),
                "captured_kont": self._kont_control_fingerprint(captured_state),
                "selected_handler_frame": self._frame_control_fingerprint(frame.selected_handler_frame),
                "outer_kont": self._kont_control_fingerprint(outer_state),
                "handler_env": frame.handler_env.bindings,
                "worker_context_ref": self._context_ref(frame.worker_context),
                "worker_context": self._context_payload(frame.worker_context),
                "handler_context_ref": self._context_ref(frame.handler_context),
                "handler_context": self._context_payload(frame.handler_context),
                "outer_context_ref": self._context_ref(frame.outer_context),
                "outer_context": self._context_payload(frame.outer_context),
                "declaration_ref": frame.declaration_ref,
                "selection_ref": frame.selection_ref,
                "resumption_handle_ref": frame.resumption_handle_ref,
                "selection_path_ref": frame.selection_path_ref,
                "captured_continuation_control_ref": (frame.captured_continuation_control_ref or captured_control_ref),
                "outer_continuation_control_ref": (frame.outer_continuation_control_ref or outer_control_ref),
                "operation_result_schema_ref": frame.operation_result_schema_ref,
                "handled_result_schema_ref": frame.handled_result_schema_ref,
            }
        if isinstance(frame, ResumeReturnFrame):
            handler_continuation = self._resume_handler_continuation_state(frame)
            handler_dynamic_tail = self._resume_handler_dynamic_tail_state(frame)
            return {
                "frame": "resume-return",
                "resume_ref": frame.resume_ref,
                "selection_path_ref": frame.selection_path_ref,
                "handler_continuation_control_ref": self._kont_control_ref(handler_continuation),
                "handler_dynamic_tail_control_ref": self._kont_control_ref(handler_dynamic_tail),
                "handler_continuation": self._kont_control_fingerprint(handler_continuation),
                "handler_return_frame": self._frame_control_fingerprint(frame.handler_return_frame),
                "handler_dynamic_tail": self._kont_control_fingerprint(handler_dynamic_tail),
                "handler_context_ref": self._context_ref(frame.handler_context),
                "handler_context": self._context_payload(frame.handler_context),
            }
        raise TypeError(f"unknown continuation frame: {frame!r}")

    def _binder_fingerprint(self, binder: BinderDef) -> object:
        return self._ensure_program_identity().binder_fingerprints[binder.binder_id]

    def _handler_env_fingerprint(self, handler_env: HandlerEnvDef) -> object:
        return self._ensure_program_identity().handler_env_fingerprints[handler_env.handler_env_ref]

    def _install_fingerprint(self, install: HandlerInstallDef) -> object:
        return self._ensure_program_identity().install_fingerprints_by_object_id[id(install)]

    def _control_fingerprint(self, control: KComputation) -> object:
        return self._ensure_program_identity().control_fingerprints[("control", id(control))]

    def _expr_fingerprint(self, expr: object) -> object:
        match expr:
            case Lit(value):
                return {"expr": "lit", "value": value}
            case Var(name):
                return {"expr": "var", "name": name}
            case RecordExpr(fields):
                return {
                    "expr": "record",
                    "fields": tuple((name, self._expr_fingerprint(value)) for name, value in fields),
                }
            case _:
                raise TypeError(f"unknown expression form: {expr!r}")

    def _schemas_fingerprint(self) -> object:
        return self._ensure_program_identity().schemas_fingerprint

    def _schema_ref_fingerprint(self, schema_ref: Ref | None) -> object | None:
        return self._ensure_program_identity().schema_ref_fingerprints[schema_ref]
