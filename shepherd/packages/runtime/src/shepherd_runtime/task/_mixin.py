from __future__ import annotations

import asyncio
import time
from contextvars import ContextVar
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, TypeVar, cast

from pydantic import BaseModel, PrivateAttr
from shepherd_core.errors import ScopeNotConfiguredError

from ..combinators.types import is_task_class
from ..step._execution import run_sync
from .checks import run_input_checks, run_output_checks
from .metadata import (
    TaskMetadata,
    _resolve_contexts,
)
from .output import (
    TaskRefReconstructionPolicy,
    extract_outputs,
    generate_output_schema,
)
from .pipeline import (
    OnError,
    OnErrorPolicy,
    Stage,
    _ContinueWithPolicy,
    _DefaultPolicy,
    _FatalPolicy,
    _make_stage_stub,
    _SkipPolicy,
)
from .prompt import generate_task_prompt

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from shepherd_core.effects import Effect
    from shepherd_core.effects.views import StreamView
    from shepherd_core.provider import Provider

    from shepherd_runtime.scope import Scope
    from shepherd_runtime.scope_types import EffectStreamLike


_T = TypeVar("_T")

# Context variable for async mode (skip auto-execute in model_post_init)
_async_mode: ContextVar[bool] = ContextVar("async_mode", default=False)

# Context variable signaling that the calling execute() is async.
# When True, @step wrappers return coroutines instead of executing synchronously.
# This is set ONLY when arun() dispatches to an async def execute().
_async_execute_mode: ContextVar[bool] = ContextVar("_async_execute_mode", default=False)


def _is_async_mode() -> bool:
    """Check if async mode is active (skip auto-execute)."""
    return _async_mode.get()


def _is_async_execute() -> bool:
    """Check if the current execute() is async (steps should return coroutines)."""
    return _async_execute_mode.get()


class TaskMixin(BaseModel):
    """Mixin providing task execution behavior."""

    # Internal state
    # Note: Typed as Any to avoid circular imports during runtime, but logic treats them correctly.
    _task_scope: Any = PrivateAttr(default=None)
    _task_name: str = PrivateAttr(default="unknown")
    _execution_result: Any = PrivateAttr(default=None)
    _cache_hit: bool = PrivateAttr(default=False)
    _taskref_policy: TaskRefReconstructionPolicy = PrivateAttr(default_factory=TaskRefReconstructionPolicy)
    _stage_name: str | None = PrivateAttr(default=None)
    _stages: dict[str, Any] = PrivateAttr(default_factory=dict)
    _projected_effects: Any = PrivateAttr(default=None)
    _kernel_v3_canary_report: Any = PrivateAttr(default=None)

    def model_post_init(self, context: Any, /) -> None:
        """Standard Pydantic hook."""
        # 1. Chain user logic first (via standard Python MRO)
        super().model_post_init(context)

        # 2. Trigger auto-execution logic
        self._auto_execute()

    def _auto_execute(self) -> None:
        """Core auto-execution logic."""
        # Store task name
        self._task_name = self.__class__.__name__
        self._task_scope = None

        # Skip if in async mode (.arun() will handle execution)
        if _is_async_mode():
            return

        # Auto-execute synchronously
        meta: TaskMetadata = self.__class__._task_meta  # type: ignore
        self._execute_sync(meta)

    def _get_taskref_policy(self) -> TaskRefReconstructionPolicy:
        """Return the current TaskRef reconstruction policy for this task instance."""
        private = object.__getattribute__(self, "__pydantic_private__")
        if private is None:
            policy = TaskRefReconstructionPolicy()
            object.__setattr__(self, "__pydantic_private__", {"_taskref_policy": policy})
            return policy

        policy = private.get("_taskref_policy")
        if policy is None:
            policy = TaskRefReconstructionPolicy()
            private["_taskref_policy"] = policy
        return cast("TaskRefReconstructionPolicy", policy)  # type: ignore[redundant-cast]

    def _set_taskref_policy(self, policy: TaskRefReconstructionPolicy) -> None:
        """Persist the TaskRef reconstruction policy on the Pydantic private state."""
        private = object.__getattribute__(self, "__pydantic_private__")
        if private is None:
            object.__setattr__(self, "__pydantic_private__", {"_taskref_policy": policy})
            return
        private["_taskref_policy"] = policy

    def _has_custom_execute(self) -> bool:
        """Check if user's class defines an execute() method."""
        # MRO traversal to find 'execute' in user's class (skipping TaskMixin/BaseModel)
        for klass in type(self).__mro__:
            if klass in (TaskMixin, BaseModel, object):
                continue
            if "execute" in klass.__dict__:
                return True
        return False

    def _execute_sync(self, meta: TaskMetadata) -> None:
        """Execute task synchronously."""
        from shepherd_runtime.scope import current_scope

        parent_scope = current_scope()
        if parent_scope is None:
            raise ScopeNotConfiguredError(
                "No scope available for task execution. Either:\n"
                "  1. Pass scope=... explicitly\n"
                "  2. Execute within 'with shepherd_runtime.scope.Scope() as scope:' block"
            )

        child_scope = parent_scope.fork()
        # Share parent's cache store so the fork can access cached results
        parent_cache = parent_scope._get_cache_store()
        if parent_cache is not None:
            child_scope._persistence_manager._cache_store = parent_cache
        try:
            with child_scope:
                self._task_scope = child_scope

                run_input_checks(self, meta)

                run_sync(self._execute_async(meta, child_scope))

                run_output_checks(self, meta)
            parent_scope.merge(child_scope)
        except Exception:
            if not child_scope.is_discarded:
                child_scope.discard()
            raise

    async def _execute_async(self, meta: TaskMetadata, scope: Scope) -> None:
        """Core async execution logic for both LLM and programmatic tasks.

        Caching is handled by the ExecutionLifecycle phases:
        - CacheCheckPhase: Checks cache before execution
        - CacheStorePhase: Stores results after execution
        """
        from ..lifecycle import ExecutionLifecycle

        # Extract inputs
        inputs = {name: getattr(self, name, None) for name in meta.inputs}

        # Resolve contexts (shared between both paths)
        explicit_contexts = {name: ctx for name in meta.contexts if (ctx := getattr(self, name, None)) is not None}
        resolved_contexts = _resolve_contexts(meta, scope, explicit_contexts)

        # Set resolved contexts on instance and bind to scope if not already bound
        for name, ctx in resolved_contexts.items():
            setattr(self, name, ctx)
            try:
                scope.get_context(name)
            except KeyError:
                # Bind may raise if the same context_id is already bound
                # under a different name (e.g. parent scope fork). Skip in
                # that case, but let other ValueErrors propagate.
                try:
                    scope.bind(name, ctx)
                except ValueError as exc:
                    if "already bound" not in str(exc):
                        raise

        _stage_name = getattr(self, "_stage_name", None)

        if self._has_custom_execute():
            # Programmatic path: route through lifecycle with executor
            is_async = asyncio.iscoroutinefunction(self.execute)
            token_exec = _async_execute_mode.set(is_async)
            # Clear _async_mode so sub-tasks instantiated inside execute()
            # can auto-execute via model_post_init. Without this, the flag
            # set by the parent arun() suppresses all nested task execution.
            token_async = _async_mode.set(False)
            try:
                async with ExecutionLifecycle(
                    scope=scope,
                    provider=None,
                    executor=self.execute,
                    kernel_v3_canary_spec=getattr(type(self), "_kernel_v3_canary_spec", None),
                    kernel_v3_canary_target=self,
                    task_name=meta.name,
                    task_meta=meta,
                    task_inputs=inputs,
                    taskref_policy=self._get_taskref_policy(),
                    stage_name=_stage_name,
                ) as lifecycle:
                    await lifecycle.run_executor()
                    self._kernel_v3_canary_report = lifecycle.kernel_v3_canary_report

                    # Deserialize device outputs if programmatic task ran on device
                    if lifecycle._device_task_outputs:
                        import pydantic

                        device_outputs = lifecycle._device_task_outputs
                        # Build Pydantic wrapper model for type-safe deserialization
                        wrapper_fields: dict[str, Any] = {}
                        for fname, finfo in meta.outputs.items():
                            if fname in device_outputs:
                                wrapper_fields[fname] = (finfo.inner_type, ...)

                        if wrapper_fields:
                            WrapperModel = pydantic.create_model("_DeviceOutputWrapper", **wrapper_fields)
                            parsed = WrapperModel.model_validate(device_outputs)
                            for fname in wrapper_fields:
                                setattr(self, fname, getattr(parsed, fname))

                    # Update contexts post-execution
                    for name in meta.contexts:
                        try:
                            updated_ctx = lifecycle.get_context(name)
                            setattr(self, name, updated_ctx)
                        except KeyError:
                            pass

                self._execution_result = None
                self._task_scope = scope
                self._cache_hit = False
            finally:
                _async_mode.reset(token_async)
                _async_execute_mode.reset(token_exec)
        else:
            # LLM path: route through lifecycle with provider
            provider = scope.get_provider()

            prompt = generate_task_prompt(meta, inputs, resolved_contexts)
            output_format = generate_output_schema(meta)

            async with ExecutionLifecycle(
                scope=scope,
                provider=provider,
                task_name=meta.name,
                artifact_markers=meta.artifact_markers,
                output_format=output_format,
                task_meta=meta,
                task_inputs=inputs,
                taskref_policy=self._get_taskref_policy(),
                stage_name=_stage_name,
            ) as lifecycle:
                result = await lifecycle.execute(prompt)

                # Check if this was a cache hit
                if lifecycle.cache_hit:
                    for name, value in lifecycle.cached_outputs.items():
                        setattr(self, name, value)
                    self._execution_result = None
                    self._task_scope = scope
                    self._cache_hit = True
                    return

                # Normal path: extract outputs from result
                outputs = extract_outputs(meta, result, taskref_policy=self._get_taskref_policy())
                for name, value in outputs.items():
                    setattr(self, name, value)

                # Set artifacts
                for name, value in lifecycle.artifact_outputs.items():
                    setattr(self, name, value)

                self._execution_result = result
                self._task_scope = scope
                self._cache_hit = False

                # Update contexts
                for name in meta.contexts:
                    try:
                        updated_ctx = lifecycle.get_context(name)
                        setattr(self, name, updated_ctx)
                    except KeyError:
                        pass

    @property
    def scope(self) -> Scope:
        """Public accessor for the task's execution scope.

        Available inside execute() for pipeline tasks to call subtasks,
        bind contexts between stages, and emit effects.

        Raises ScopeNotConfiguredError if accessed before execution.
        """
        private = object.__getattribute__(self, "__pydantic_private__")
        if private:
            task_scope = private.get("_task_scope")
            if task_scope is not None:
                return task_scope  # type: ignore[no-any-return]
        raise ScopeNotConfiguredError(
            f"Task '{type(self).__name__}' has no scope. The scope is set during task execution (sync or async)."
        )

    @property
    def stages(self) -> MappingProxyType[str, Any]:
        """Read-only dict of stage name -> completed task instance (or None).

        Populated by run_stage() calls during execute(). Returns an empty
        mapping for non-pipeline tasks.
        """
        private = object.__getattribute__(self, "__pydantic_private__")
        if private and "_stages" in private:
            return MappingProxyType(private["_stages"])
        return MappingProxyType({})

    @property
    def effects(self) -> EffectStreamLike:
        """Access the effect stream from this task's execution.

        If ``with_view()`` has been used, returns the projected stream.
        Otherwise returns the full stream from the execution scope.
        """
        from shepherd_runtime.scope_types import create_stream

        # Access Pydantic private attr directly to avoid __getattr__ cascade
        # (Pydantic private attrs are stored in __pydantic_private__, not __dict__)
        private = object.__getattribute__(self, "__pydantic_private__")
        if private:
            # Check for projected stream first (set by with_view)
            projected = private.get("_projected_effects")
            if projected is not None:
                return projected
            task_scope = private.get("_task_scope")
            if task_scope is not None:
                return task_scope.effects
        return create_stream()

    @property
    def kernel_v3_canary_report(self) -> Any:
        """Return the kernel-v3 canary report for this execution, if any."""
        private = object.__getattribute__(self, "__pydantic_private__")
        if private:
            return private.get("_kernel_v3_canary_report")
        return None

    @property
    def task_ref(self) -> type:
        """The task class (TaskRef) this instance was executed from.

        Provides the CompletedTask -> TaskRef accessor, returning the
        underlying @task class that can be used for source extraction
        or as an Input(TaskRef) value.
        """
        return type(self)

    def with_view(
        self,
        *views: str | Callable[[EffectStreamLike], EffectStreamLike | StreamView],
        include: Sequence[type[Effect]] | None = None,
        exclude: Sequence[type[Effect]] | None = None,
    ) -> Any:
        """Return a copy with effects projected through the given view(s).

        The original instance is not modified. The copy shares the same
        scope and execution data, but ``.effects`` returns the projected
        stream instead of the full stream.

        Args:
            *views: Named views (``"thinking"``, ``"intents"``, ``"outcomes"``)
                or callable projections (``lambda s: s.filter(...)``).
                Multiple names are unioned. Available names: ``thinking``,
                ``intents``, ``outcomes``, ``costs``.
            include: Keep only effects of these types.
            exclude: Remove effects of these types.

        Returns:
            A shallow copy of this instance with projected effects.

        Example::

            # Named views (union of multiple)
            result.with_view("thinking", "intents", "outcomes")

            # Single named view
            result.with_view("thinking")

            # Exclude lifecycle noise
            from shepherd_core.effects import LifecyclePhaseStarted, LifecyclePhaseCompleted

            result.with_view(exclude=[LifecyclePhaseStarted, LifecyclePhaseCompleted])

            # Custom callable (escape hatch)
            result.with_view(lambda s: s.filter(my_predicate))
        """
        from shepherd_runtime.scope_types import create_stream

        stream = self.effects
        projected: EffectStreamLike

        if views:
            if len(views) == 1 and callable(views[0]):
                # Single callable — original simple path
                view_result = views[0](stream)
                if hasattr(view_result, "to_stream"):
                    projected = view_result.to_stream()
                else:
                    projected = view_result
            else:
                # Union of named views and/or callables
                kept_sequences: set[int] = set()
                for v in views:
                    if callable(v):
                        view_result = v(stream)
                    elif isinstance(v, str):
                        view_fn = getattr(stream, v, None)
                        if view_fn is None:
                            raise ValueError(f"Unknown view: {v!r}. Available: thinking, intents, outcomes, costs")
                        view_result = view_fn()
                    else:
                        raise TypeError(f"Expected view name (str) or callable, got {type(v).__name__}")
                    for layer in view_result:
                        kept_sequences.add(layer.sequence)
                projected = create_stream(tuple(layer for layer in stream if layer.sequence in kept_sequences))
        elif include is not None:
            include_tuple = tuple(include)
            projected = stream.filter(lambda e: isinstance(e, include_tuple))
        elif exclude is not None:
            exclude_tuple = tuple(exclude)
            projected = stream.filter(lambda e: not isinstance(e, exclude_tuple))
        else:
            raise ValueError("with_view() requires at least one view name, callable, include, or exclude argument")

        # Apply exclude as a second pass if combined with views/include
        if exclude is not None and views:
            exclude_tuple = tuple(exclude)
            projected = projected.filter(lambda e: not isinstance(e, exclude_tuple))

        # Shallow copy: new private dict, shared scope reference
        copy = self.model_copy()
        private = object.__getattribute__(copy, "__pydantic_private__")
        private["_projected_effects"] = projected
        return copy

    def __getattr__(self, name: str) -> Any:
        """Provide helpful error messages for common attribute mistakes."""
        if name == "rejected":
            raise AttributeError(
                f"'{type(self).__name__}' has no attribute 'rejected'. "
                f"The .rejected property is only available on PipelineResult "
                f"when using Pipeline().gate(). For direct task instantiation, "
                f"tasks execute immediately and never have rejection status."
            )
        raise AttributeError(f"'{type(self).__name__}' object has no attribute '{name}'")

    async def run_stage(
        self,
        name: str,
        task_class: type[_T] | Any,
        *,
        retry: int = 0,
        timeout: float | None = None,
        device: str | None = None,
        on_error: OnErrorPolicy | None = None,
        **inputs: Any,
    ) -> _T:
        """Execute a named stage within a pipeline task's execute() method.

        Emits StageStarted/StageCompleted/StageSkipped/StageFailed effects,
        applies retry and timeout, manages per-stage device contexts, and
        handles OnError policies.

        Accepts either a ``@task`` class or a combinator-wrapped callable.
        Combinator callables follow the ``(inputs: dict, scope) -> result``
        signature used by ``gate()``, ``retry()``, ``timeout()``, etc.

        Args:
            name: Unique stage identifier for effects and the stages registry.
            task_class: The @task class or combinator-wrapped callable to execute.
            retry: Number of retry attempts after initial failure (0 = no retry).
            timeout: Execution timeout in seconds (None = no timeout).
            device: Device name for this stage (e.g., "container"). If the
                requested device matches the ambient Device context, the
                ambient device is reused (no nesting error). Different-device
                nesting still raises DeviceNestingError.
            on_error: Error policy if the stage fails (default: OnError.fatal).
            **inputs: Keyword arguments passed to the subtask.

        Returns:
            The completed task instance, a stub (for default/continue_with),
            or None (for skip).

        Example::

            parsed = await self.run_stage("parse", ParseDocument, raw_text=self.raw_text)

            classified = await self.run_stage(
                "classify",
                ClassifyDocument,
                first_paragraph=parsed.first_paragraph,
                word_count=parsed.word_count,
                retry=1,
                on_error=OnError.default(category="other"),
            )

            # Combinator-wrapped task:
            from shepherd_core.combinators import gate

            reviewed = await self.run_stage(
                "review",
                gate(ReviewPR, quality_check),
                details=pr.details,
            )
        """
        from shepherd_core.effects.effects import StageCompleted, StageFailed, StageSkipped, StageStarted

        from ..device import Device as DeviceCtx

        if on_error is None:
            on_error = OnError.fatal

        private = object.__getattribute__(self, "__pydantic_private__")
        stages_dict: dict[str, Any] = private["_stages"]

        # Enforce unique stage names
        if name in stages_dict:
            raise ValueError(
                f"Duplicate stage name '{name}'. Each run_stage call must have a unique name. "
                f"For dynamic stages, use f-strings: run_stage(f'process_{{i}}', ...)"
            )

        pipeline_scope = self.scope
        # Access _task_name via __pydantic_private__ (same bypass as scope property)
        task_name = private.get("_task_name", "unknown")
        pipeline_scope.emit(StageStarted(stage_name=name, task_name=task_name))
        start_time = time.time()

        async def _execute_stage() -> Any:
            if is_task_class(task_class):
                coro = task_class.arun(scope=pipeline_scope, stage_name=name, **inputs)  # type: ignore[union-attr]
            else:
                # Combinator-wrapped callable: (inputs_dict, scope) -> result
                coro = task_class(inputs, pipeline_scope)  # type: ignore[call-arg]
            if timeout:
                return await asyncio.wait_for(coro, timeout=timeout)
            return await coro

        last_error: Exception | None = None

        for attempt in range(retry + 1):
            try:
                if device:
                    # Reuse ambient device if it matches the requested one,
                    # avoiding DeviceNestingError for same-device run_stage calls
                    from ..device import get_current_device

                    ambient = get_current_device()
                    if ambient is not None and ambient.name == device:
                        result = await _execute_stage()
                    else:
                        with DeviceCtx(device):
                            result = await _execute_stage()
                else:
                    result = await _execute_stage()

                duration = (time.time() - start_time) * 1000
                pipeline_scope.emit(StageCompleted(stage_name=name, task_name=task_name, duration_ms=duration))
                stages_dict[name] = result
                return result  # type: ignore[no-any-return]  # arun returns Any at runtime

            except Exception as e:  # noqa: BLE001
                last_error = e
                if attempt < retry:
                    pipeline_scope.emit(
                        StageFailed(
                            stage_name=name,
                            task_name=task_name,
                            error=f"Attempt {attempt + 1}/{retry + 1}: {str(e)[:480]}",
                            duration_ms=(time.time() - start_time) * 1000,
                        )
                    )

        # All attempts exhausted — apply error policy
        duration = (time.time() - start_time) * 1000
        error_msg = str(last_error)[:500] if last_error else ""

        if isinstance(on_error, _FatalPolicy):
            pipeline_scope.emit(
                StageFailed(stage_name=name, task_name=task_name, error=error_msg, duration_ms=duration)
            )
            stages_dict[name] = None
            raise last_error  # type: ignore[misc]

        if isinstance(on_error, _SkipPolicy):
            pipeline_scope.emit(StageSkipped(stage_name=name, task_name=task_name, reason=error_msg))
            stages_dict[name] = None
            return None  # type: ignore[return-value]  # skip policy returns None by design

        if isinstance(on_error, _DefaultPolicy):
            stub = _make_stage_stub(name, **on_error.values)
            pipeline_scope.emit(
                StageCompleted(
                    stage_name=name,
                    task_name=task_name,
                    duration_ms=duration,
                    defaulted=True,
                )
            )
            stages_dict[name] = stub
            return stub  # type: ignore[return-value]  # stub is structurally compatible with _T

        if isinstance(on_error, _ContinueWithPolicy):
            stub = _make_stage_stub(name, **on_error.values)
            pipeline_scope.emit(
                StageCompleted(
                    stage_name=name,
                    task_name=task_name,
                    duration_ms=duration,
                    partial=True,
                )
            )
            stages_dict[name] = stub
            return stub  # type: ignore[return-value]  # stub is structurally compatible with _T

        # Unreachable for valid OnError variants
        raise last_error  # type: ignore[misc]

    def run_stage_sync(
        self,
        name: str,
        task_class: type[_T] | Any,
        *,
        retry: int = 0,
        timeout: float | None = None,
        device: str | None = None,
        on_error: OnErrorPolicy | None = None,
        **inputs: Any,
    ) -> _T:
        """Sync version of run_stage() for use in sync execute() methods.

        Bridges to the async run_stage() via run_sync(), providing the same
        stage effects (StageStarted/StageCompleted/StageFailed/StageSkipped),
        retry, timeout, and OnError policy support.

        Example::

            def execute(self) -> None:
                critique = self.run_stage_sync(
                    "critique",
                    CritiqueDocuments,
                    on_error=OnError.skip,
                    document_paths=self.document_paths,
                )
        """
        return run_sync(
            self.run_stage(
                name,
                task_class,
                retry=retry,
                timeout=timeout,
                device=device,
                on_error=on_error,
                **inputs,
            )
        )

    async def run_stages_parallel(
        self,
        *stages: Stage,
        max_concurrency: int | None = None,
    ) -> list[Any]:
        """Execute multiple stages in parallel with fork/merge isolation.

        Each stage runs in a forked scope. Successful stages are merged back;
        failed stages are discarded (with the stage's on_error policy applied).
        StageStarted/StageCompleted/StageSkipped/StageFailed effects are
        emitted for each stage, just like sequential run_stage() calls.

        Stages are batched by ``max_concurrency`` (None = all at once).
        Results are returned in the same order as the input stages;
        ``None`` entries correspond to skipped/failed stages.

        Args:
            *stages: Stage descriptors (name, task_class, inputs, on_error).
            max_concurrency: Max stages to run simultaneously.

        Returns:
            List of results in input order.

        Example::

            results = await self.run_stages_parallel(
                Stage("doc_gaps", AnalyzeCode, {"concern": "docs", ...}),
                Stage("correctness", AnalyzeCode, {"concern": "correctness", ...}),
                max_concurrency=2,
            )
        """
        from shepherd_core.effects.effects import StageCompleted, StageFailed, StageSkipped, StageStarted

        private = object.__getattribute__(self, "__pydantic_private__")
        stages_dict: dict[str, Any] = private["_stages"]
        task_name = private.get("_task_name", "unknown")
        pipeline_scope = self.scope

        # Validate unique names (across existing stages and within this batch)
        seen: set[str] = set()
        for stage in stages:
            if stage.name in stages_dict or stage.name in seen:
                raise ValueError(f"Duplicate stage name '{stage.name}'. Each run_stage call must have a unique name.")
            seen.add(stage.name)

        results: list[Any] = [None] * len(stages)
        batch_size = max_concurrency or len(stages)

        for batch_start in range(0, len(stages), batch_size):
            batch = stages[batch_start : batch_start + batch_size]

            forks_and_coros: list[tuple[int, Stage, Any, Any]] = []
            for offset, stage in enumerate(batch):
                idx = batch_start + offset
                fork = pipeline_scope.fork()
                pipeline_scope.emit(StageStarted(stage_name=stage.name, task_name=task_name))
                if is_task_class(stage.task_class):
                    coro = stage.task_class.arun(  # type: ignore[union-attr]
                        scope=fork,
                        stage_name=stage.name,
                        **stage.inputs,
                    )
                else:
                    # Combinator-wrapped callable: (inputs_dict, scope) -> result
                    coro = stage.task_class(stage.inputs, fork)
                forks_and_coros.append((idx, stage, fork, coro))

            batch_start_time = time.time()
            gathered = await asyncio.gather(
                *[coro for _, _, _, coro in forks_and_coros],
                return_exceptions=True,
            )
            batch_duration_ms = int((time.time() - batch_start_time) * 1000)

            for (idx, stage, fork, _coro), result in zip(forks_and_coros, gathered, strict=True):
                if isinstance(result, BaseException):
                    fork.discard()
                    on_error = stage.on_error

                    error_msg = str(result)[:500]

                    if isinstance(on_error, _FatalPolicy):
                        pipeline_scope.emit(
                            StageFailed(
                                stage_name=stage.name,
                                task_name=task_name,
                                error=error_msg,
                                duration_ms=batch_duration_ms,
                            )
                        )
                        stages_dict[stage.name] = None
                        raise result

                    if isinstance(on_error, _SkipPolicy):
                        pipeline_scope.emit(StageSkipped(stage_name=stage.name, task_name=task_name, reason=error_msg))
                        stages_dict[stage.name] = None
                        results[idx] = None

                    elif isinstance(on_error, (_DefaultPolicy, _ContinueWithPolicy)):
                        stub = _make_stage_stub(stage.name, **on_error.values)
                        defaulted = isinstance(on_error, _DefaultPolicy)
                        pipeline_scope.emit(
                            StageCompleted(
                                stage_name=stage.name,
                                task_name=task_name,
                                duration_ms=batch_duration_ms,
                                defaulted=defaulted,
                                partial=not defaulted,
                            )
                        )
                        stages_dict[stage.name] = stub
                        results[idx] = stub
                else:
                    pipeline_scope.merge(fork)
                    pipeline_scope.emit(
                        StageCompleted(stage_name=stage.name, task_name=task_name, duration_ms=batch_duration_ms)
                    )
                    stages_dict[stage.name] = result
                    results[idx] = result

        return results

    @classmethod
    async def arun(
        cls,
        scope: Scope | None = None,
        provider: Provider | str | None = None,
        taskref_policy: TaskRefReconstructionPolicy | None = None,
        stage_name: str | None = None,
        **kwargs: Any,
    ) -> Any:
        # Avoid circular import
        from shepherd_runtime.scope import current_scope

        # Meta is stored on the class by the decorator
        meta: TaskMetadata = cls._task_meta  # type: ignore

        parent_scope = scope or current_scope()
        if parent_scope is None:
            raise ScopeNotConfiguredError(
                "No scope available. Pass scope=... or use async with shepherd_runtime.scope.Scope()."
            )

        child_scope = parent_scope.fork()
        # Share parent's cache store so the fork can access cached results
        parent_cache = parent_scope._get_cache_store()
        if parent_cache is not None:
            child_scope._persistence_manager._cache_store = parent_cache

        try:
            async with child_scope:
                # Signal async mode to skip auto-execute in model_post_init
                token = _async_mode.set(True)
                try:
                    # Create instance WITHOUT auto-execution
                    instance = cls(**kwargs)
                    instance._task_scope = child_scope
                    instance._stage_name = stage_name
                    instance._set_taskref_policy(taskref_policy or TaskRefReconstructionPolicy())

                    run_input_checks(instance, meta)

                    # Unified dispatch: both programmatic and LLM tasks
                    # go through _execute_async, which routes to the lifecycle
                    await instance._execute_async(meta, child_scope)

                    run_output_checks(instance, meta)
                finally:
                    _async_mode.reset(token)
            parent_scope.merge(child_scope)
            return instance
        except Exception:
            if not child_scope.is_discarded:
                child_scope.discard()
            raise

    @classmethod
    def run(
        cls,
        *,
        taskref_policy: TaskRefReconstructionPolicy | None = None,
        **kwargs: Any,
    ) -> Any:
        """Backward compatibility."""
        return cls.arun(taskref_policy=taskref_policy, **kwargs)

    @classmethod
    def run_sync(
        cls,
        *,
        taskref_policy: TaskRefReconstructionPolicy | None = None,
        **kwargs: Any,
    ) -> Any:
        """Backward compatibility."""
        return asyncio.run(cls.arun(taskref_policy=taskref_policy, **kwargs))
