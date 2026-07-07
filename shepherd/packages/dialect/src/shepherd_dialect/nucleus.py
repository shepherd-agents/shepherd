"""The dialect's user vocabulary — the quickstart contract, re-pinned (W1).

The Appendix-C callable spine (`workspace()` / ``@task`` / ``deliver`` /
``Run[T]`` / ``RunRef``) composed strictly over the landed primitives: every
task call routes through ``execute_recorded("runtime", "run", …)`` (the
reversible wrap; durable trace per slice 1), and ``run.trace`` reads back
through the slice-3 public route.

Shape is bound by S1 (`spikes/260610-quickstart-probe/FINDINGS.md`):

- ``workspace(model=…, root=…)`` is **ambient module state** (not a context
  manager); it builds the VcsCore composition eagerly (probe b: 0.03s) and
  returns a ``Workspace`` handle. Conflict → ``WorkspaceAlreadyConfigured``;
  task call without one → ``WorkspaceNotConfigured``;
  ``reset_workspace_for_tests()`` deactivates and drops.
- ``deliver(Type, goal=…, evidence=…)`` is the **in-body** model-delivery
  verb: it resolves via an installed ``handle("model.call", responder)``
  responder (the offline pattern) or the workspace's model. Missing/malformed
  structured output → ``Failed`` (the quickstart's no-result-key observable).
- Plain task calls **unwrap** — raising ``DeliveryFailed`` (carrying ``.run``)
  on non-``Finished``; ``.detailed()`` returns the ``Run`` and never raises.
- Outcome mapping (D3, ratified): ``Finished``=merged · ``Failed``=body
  raise / jail or provider refusal · ``Stopped``=``SupervisorDenied`` ·
  ``Exhausted``=positively identified budget signal only
  (``BudgetExhausted``; probe a — ambiguous alarm kills stay ``Failed``).
- Sync-first (D2): the core path is sync; async bodies ride a thin wrapper.

Owner-path discipline: the outcome variants and workspace errors live HERE
(not on the package facade) — the quickstart's export tests pin both lists.
"""

from __future__ import annotations

import asyncio
import contextlib
import contextvars
import inspect
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Generic, TypeVar

from shepherd_kernel_v3_reference.proof_envelope import ProofEnvelope, runtime_only_envelope

from shepherd_dialect.checks import CheckFailed, extract_checks, run_checks

T = TypeVar("T")

__all__ = [
    "Artifact",
    "BudgetExhausted",
    "DeliveryFailed",
    "EffectNotPermitted",
    "Exhausted",
    "Failed",
    "Finished",
    "NoActiveTaskRun",
    "Run",
    "RunRef",
    "Stopped",
    "Workspace",
    "WorkspaceAlreadyConfigured",
    "WorkspaceNotConfigured",
    "ask",
    "current_binding",
    "deliver",
    "emit_artifact",
    "handle",
    "reset_workspace_for_tests",
    "task",
    "tell",
    "workspace",
]


# --- outcome variants (CONTRACTS A2/A6 — frozen, owner-path) -----------------


@dataclass(frozen=True)
class Finished(Generic[T]):
    """The run merged; ``value`` is the typed result."""

    value: T


@dataclass(frozen=True)
class Exhausted:
    """A positively identified budget stop (D3: never inferred from ambiguity)."""

    reason: str


@dataclass(frozen=True)
class Stopped:
    """Supervision or cancellation ended the run; the wrap discarded."""

    reason: str


@dataclass(frozen=True)
class Failed:
    """Body raise, jail refusal, or provider refusal; the wrap discarded."""

    error_type: str
    message: str
    retryable: bool | None = None


class DeliveryFailed(Exception):  # noqa: N818 — the spec's pinned name (CONTRACTS A7)
    """Plain-call unwrap of a non-Finished outcome; carries the Run (A7)."""

    def __init__(self, message: str, *, run: Run[Any]) -> None:
        super().__init__(message)
        self.run = run


class BudgetExhausted(Exception):  # noqa: N818 — names the outcome, not an error class
    """A positively identified budget stop (probe a: 'Reached max turns')."""


class WorkspaceAlreadyConfigured(Exception):  # noqa: N818 — the spec's pinned name
    """A conflicting ambient workspace is already configured."""


class WorkspaceNotConfigured(Exception):  # noqa: N818 — the spec's pinned name
    """A task ran before ``workspace(...)`` configured the ambient workspace."""


class NoActiveTaskRun(Exception):  # noqa: N818 — the spec's pinned name
    """An in-body verb was called outside a task run."""


class EffectNotPermitted(Exception):  # noqa: N818 — the spec's pinned name
    """A run attempted an in-process effect outside its coarse ``may=`` profile."""


class ReservedRuntimeParameter(Exception):  # noqa: N818 — the spec's pinned outcome name
    """A caller attempted to provide a runtime-owned task parameter."""


# --- Run / RunRef / Artifact --------------------------------------------------


@dataclass(frozen=True)
class RunRef:
    """The run's stable reference (CONTRACTS A3; ``id`` starts ``run-``)."""

    id: str


@dataclass(frozen=True)
class Artifact:
    """A named artifact the body emitted (``emit_artifact``; CONTRACTS A1)."""

    name: str
    content: bytes


@dataclass(frozen=True)
class Run(Generic[T]):
    """The public outcome value (CONTRACTS A2): outcome + ref + duration + artifacts."""

    outcome: Finished[T] | Exhausted | Stopped | Failed
    ref: RunRef
    duration: float
    artifacts: tuple[Artifact, ...] = ()
    proof: ProofEnvelope = field(default_factory=runtime_only_envelope)
    _trace_head: str | None = field(default=None, repr=False)
    _trace_payload: Any | None = field(default=None, repr=False)

    def unwrap(self) -> T:
        if isinstance(self.outcome, Finished):
            return self.outcome.value
        raise DeliveryFailed(f"run {self.ref.id} ended {type(self.outcome).__name__}", run=self)

    @property
    def trace(self) -> Any:  # RunTrace | None — the slice-3 read, lazy
        """The materialized durable trace, or a Path-A in-memory logical child trace."""
        if self._trace_payload is not None:
            from shepherd_dialect.trace import RunTrace

            return RunTrace(self._trace_payload)
        if self._trace_head is None:
            return None
        from shepherd_dialect.trace import read_run_trace

        ws = _ambient()
        return read_run_trace(ws._mg, self._trace_head)


# --- the ambient workspace -----------------------------------------------------


class Workspace:
    """The ambient workspace handle: owns the VcsCore composition (probe b: eager)."""

    def __init__(self, *, model: Any, root: Path) -> None:
        from vcs_core.runtime_api import Store, VcsCore, build_builtin_substrate_context
        from vcs_core.runtime_substrate import TaskTraceSubstrateDriver
        from vcs_core.substrates import DeclarativeFilesystemSubstrate, MarkerSubstrate

        from shepherd_dialect.run_driver import ShepherdRunDriver
        from shepherd_dialect.workspace_control.drivers import (
            ShepherdRunLedgerDriver,
            ShepherdTaskArtifactDriver,
            ShepherdTaskLedgerDriver,
        )

        self.model = model
        self.root = root
        self.trace_store_path = root / ".vcscore" / "shepherd" / "trace.sqlite"
        store = Store(str(root / ".vcscore"))
        ctx = build_builtin_substrate_context(store, workspace=root, config={"backend": "clonefile"})
        self._mg = VcsCore(
            str(root),
            substrates=[
                MarkerSubstrate(ctx),
                DeclarativeFilesystemSubstrate(ctx),
                ShepherdTaskLedgerDriver(),
                ShepherdTaskArtifactDriver(),
                ShepherdRunLedgerDriver(),
                ShepherdRunDriver(),
                TaskTraceSubstrateDriver(),
            ],
            store=store,
        )
        self._mg.activate()
        self._scope = self._mg.ground

    @property
    def scope(self) -> Any:
        """Idempotent scope reads — never forks (CONTRACTS A1/D4)."""
        return self._scope


@dataclass
class _RunContext:
    """Run-local state that must compose across nesting and async thread hops."""

    ref: RunRef
    parent_ref: str | None
    may_profile: str
    may_source: str
    artifacts: list[Artifact] = field(default_factory=list)
    step_events: list[dict[str, Any]] = field(default_factory=list)
    safe_point: str = "running"


_GLOBAL: dict[str, Workspace | None] = {"ws": None}
_CURRENT_RUN: contextvars.ContextVar[_RunContext | None] = contextvars.ContextVar(
    "shepherd_dialect_current_run", default=None
)
_RUN_REGISTRY: dict[str, _RunContext] = {}
_RESPONDERS: contextvars.ContextVar[tuple[tuple[str, Any], ...]] = contextvars.ContextVar(
    "shepherd_dialect_responders", default=()
)
_PROFILE_IN_PROCESS_EFFECTS: dict[str, frozenset[str] | None] = {
    "Permissive": None,  # all in-process responder effects are allowed.
    "ReadOnly": frozenset({"operator.decision"}),  # operator control is not a workspace/provider effect.
}
_MAY_DOMINANCE: dict[str, frozenset[str]] = {
    "Permissive": frozenset({"Permissive", "ReadOnly"}),
    "ReadOnly": frozenset({"ReadOnly"}),
}


def _emit_step_event(event: dict[str, Any]) -> None:
    """Record a step lifecycle event into the run's durable trace (W2b).

    A no-op outside a run so steps stay unit-testable (S1 seam 2).
    """
    ctx = _CURRENT_RUN.get()
    if ctx is not None:
        ctx.step_events.append(event)


def _run_coro(coro: Any) -> Any:
    """Run a coroutine to completion whether or not a loop is already running."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    import concurrent.futures

    ctx = contextvars.copy_context()
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(ctx.run, asyncio.run, coro).result()


def workspace(*, model: Any, root: str) -> Workspace:
    """Configure (or idempotently return) the ambient workspace."""
    resolved = Path(root).expanduser().resolve()
    current = _GLOBAL["ws"]
    if current is not None:
        if current.model == model and current.root == resolved:
            return current
        raise WorkspaceAlreadyConfigured(
            f"ambient workspace already configured at {current.root} with {current.model!r}"
        )
    ws = Workspace(model=model, root=resolved)
    _GLOBAL["ws"] = ws
    return ws


def _ambient() -> Workspace:
    ws = _GLOBAL["ws"]
    if ws is None:
        raise WorkspaceNotConfigured("call workspace(model=…, root=…) before running tasks")
    return ws


def reset_workspace_for_tests() -> None:
    """Deactivate and drop the ambient workspace + responders (test hook)."""
    ws = _GLOBAL["ws"]
    if ws is not None:
        with contextlib.suppress(Exception):
            ws._mg.deactivate()
    _GLOBAL["ws"] = None
    _RUN_REGISTRY.clear()
    _RESPONDERS.set(())


# --- handle("model.call", responder) + in-body verbs ---------------------------


def _responders() -> list[tuple[str, Any]]:
    return list(_RESPONDERS.get())


# Dual-key compatibility shim (Bug 1, 2132 W0.1): accept the taught
# handle("model.call.requested", ...) spelling by normalizing it onto the
# dispatch key at installation. Dispatch and recorded vocabulary stay
# "model.call" (the kind-string bump is a durable-vocabulary decision, D-3).
# This fence also keeps the dialect quickstart nucleus from reintroducing a
# key drift between the two spellings.
_EFFECT_KEY_ALIASES: dict[str, str] = {"model.call.requested": "model.call"}


@contextlib.contextmanager
def handle(effect: str, responder: Any) -> Any:
    """Install an in-process responder (the offline `model.call` pattern)."""
    current = _RESPONDERS.get()
    token = _RESPONDERS.set((*current, (_EFFECT_KEY_ALIASES.get(effect, effect), responder)))
    try:
        yield
    finally:
        _RESPONDERS.reset(token)


def _dispatch(effect: str, request: Any) -> Any:
    ctx = _CURRENT_RUN.get()
    if ctx is not None:
        allowed = _PROFILE_IN_PROCESS_EFFECTS.get(ctx.may_profile, frozenset())
        if allowed is not None and effect not in allowed:
            raise EffectNotPermitted(
                f"{effect!r} is not permitted under may={ctx.may_profile!r} at in-process dispatch"
            )
    for name, responder in reversed(_responders()):
        if name == effect:
            return responder(request)
    raise NoActiveTaskRun(f"no responder installed for {effect!r} and no live provider path in v1")


def ask(effect: str, request: Any) -> Any:
    """v1: dispatch to installed in-process responders only (Phase E owns the command lane)."""
    return _dispatch(effect, request)


def tell(effect: str, payload: Any) -> None:
    """Fire-and-forget notify: dispatch to a responder if one is installed."""
    with contextlib.suppress(NoActiveTaskRun):
        _dispatch(effect, payload)


def current_binding() -> Workspace:
    """The ambient workspace the current task is bound to."""
    return _ambient()


def deliver(output_type: type[T], *, goal: str, evidence: list[Any] | None = None) -> T:
    """The in-body model-delivery verb: typed value via the model seam."""
    from shepherd_dialect.provider_boundary import ModelRequest

    response = _dispatch("model.call", ModelRequest(goal=goal, evidence=tuple(evidence or ())))
    structured = getattr(response, "structured_output", response)
    if not isinstance(structured, dict) or "result" not in structured:
        raise _DeliveryShapeError(f"model response missing the result key: {structured!r}")
    return output_type(**structured["result"])


class _DeliveryShapeError(Exception):
    pass


def emit_artifact(name: str, content: bytes | str) -> None:
    """Attach a named artifact to the current run (lands in ``Run.artifacts``)."""
    ctx = _CURRENT_RUN.get()
    if ctx is None:
        raise NoActiveTaskRun("emit_artifact outside a task run")
    ctx.artifacts.append(Artifact(name=name, content=content.encode() if isinstance(content, str) else content))


def _may_dominates(parent_profile: str, child_profile: str) -> bool:
    return child_profile in _MAY_DOMINANCE.get(parent_profile, frozenset())


def _caller_supplied_parameter(signature: inspect.Signature, args: tuple, kwargs: dict, name: str) -> bool:
    if name in kwargs:
        return True
    positional_index = 0
    for parameter in signature.parameters.values():
        if parameter.kind not in (
            inspect.Parameter.POSITIONAL_ONLY,
            inspect.Parameter.POSITIONAL_OR_KEYWORD,
        ):
            continue
        if parameter.name == name:
            return len(args) > positional_index
        positional_index += 1
    return False


# --- @task ---------------------------------------------------------------------


def _execute(
    fn: Any,
    args: tuple,
    kwargs: dict,
    *,
    may: str | None,
    success_disposition: str = "merge",
) -> Run[Any]:
    if success_disposition not in {"merge", "seal"}:
        raise ValueError(f"unsupported success disposition: {success_disposition!r}")
    from shepherd_dialect.confinement import resolve_may
    from shepherd_dialect.supervision import SupervisorDenied

    ws = _ambient()
    started = time.monotonic()
    from shepherd_dialect.workspace_control.run_ledger import utc_now

    started_at = utc_now()
    ref = RunRef(id=f"run-{uuid.uuid4().hex[:12]}")
    parent_ctx = _CURRENT_RUN.get()
    may_resolution = resolve_may(may)
    run_ctx = _RunContext(
        ref=ref,
        parent_ref=parent_ctx.ref.id if parent_ctx is not None else None,
        may_profile=may_resolution.resolved,
        may_source=may_resolution.source,
    )
    _RUN_REGISTRY[ref.id] = run_ctx
    run_token = _CURRENT_RUN.set(run_ctx)
    task_id = f"{getattr(fn, '__module__', '?')}:{getattr(fn, '__qualname__', '?')}"
    input_world = ws._mg.world_oid()
    input_checks, output_checks = extract_checks(fn)
    violation: CheckFailed | None = None
    runtime_operation_id: str | None = None
    runtime_scope_ref: str | None = None
    body_entered = False
    setup_failure = False
    launch_refused = False
    run_record_published = False
    sealed_execution: Any | None = None
    run_scope = ws.scope

    fn_signature = inspect.signature(fn)
    fn_accepts_working_path = "working_path" in fn_signature.parameters

    def body(stack: Any, *, working_path: str | None = None, **_params: Any) -> Any:
        nonlocal body_entered
        body_entered = True
        if _caller_supplied_parameter(fn_signature, args, kwargs, "working_path"):
            raise ReservedRuntimeParameter("'working_path' is runtime-owned; do not pass it to @task calls")
        call_kwargs = kwargs
        if working_path is not None and fn_accepts_working_path and "working_path" not in call_kwargs:
            call_kwargs = {**kwargs, "working_path": working_path}
        result = fn(*args, **call_kwargs)
        if inspect.iscoroutine(result):  # D2: thin async wrapper over the sync core
            result = _run_coro(result)
        for chk in output_checks:  # postconditions: raise inside the body -> the wrap discards
            if not chk(result):
                raise CheckFailed(task_id, "return", result, chk, "postcondition")
        return result

    outcome: Finished[Any] | Exhausted | Stopped | Failed
    try:
        # Preconditions refuse BEFORE the fork (S1 seam 1): raising here means
        # execute_recorded is never reached — no run scope, no carrier cost.
        run_checks(task_id, fn, args, kwargs, input_checks)
        if parent_ctx is not None:
            if not _may_dominates(parent_ctx.may_profile, may_resolution.resolved):
                launch_refused = True
                outcome = Failed(
                    error_type="EffectNotPermitted",
                    message=(
                        f"parent may={parent_ctx.may_profile!r} cannot launch child may={may_resolution.resolved!r}"
                    ),
                )
            else:
                runtime_operation_id = f"logical-child:{ref.id}"
                runtime_scope_ref = f"refs/shepherd/logical-runs/{ref.id}"
                outcome = Finished(value=body(None))
        else:
            run_params: dict[str, Any] = {"task_body": body}
            if may_resolution.declared is not None:
                run_params["may"] = may_resolution.declared
            if success_disposition == "seal":
                from vcs_core.runtime_api import CommandExecutionOptions

                run_params["execution_options"] = CommandExecutionOptions(success_disposition="seal")
            try:
                recorded = ws._mg.execute_recorded("runtime", "run", scope=run_scope, **run_params)
            except Exception as exc:
                if body_entered:
                    raise
                setup_failure = True
                outcome = Failed(error_type=type(exc).__name__, message=str(exc))
            else:
                driver_result = recorded.value
                if success_disposition == "seal":
                    from vcs_core.types import SealedExecutionOutcome

                    if not isinstance(recorded.value, SealedExecutionOutcome):
                        raise RuntimeError("seal success disposition returned no sealed execution outcome")
                    sealed_execution = recorded.value
                    driver_result = sealed_execution.driver_result
                portable_core = driver_result.transitions[0].payload["portable_core"]
                raw_operation_id = portable_core.get("operation_id")
                runtime_operation_id = raw_operation_id if isinstance(raw_operation_id, str) else None
                raw_run_scope = portable_core.get("run_scope")
                if isinstance(raw_run_scope, dict):
                    raw_scope_ref = raw_run_scope.get("scope_ref")
                    runtime_scope_ref = raw_scope_ref if isinstance(raw_scope_ref, str) else None
                value = portable_core["outcome"]["result"]
                outcome = Finished(value=value)
    except CheckFailed as exc:
        violation = exc
        outcome = Failed(error_type="CheckFailed", message=str(exc))
    except SupervisorDenied as exc:
        outcome = Stopped(reason=exc.reason)
    except BudgetExhausted as exc:
        outcome = Exhausted(reason=str(exc))
    except _DeliveryShapeError as exc:
        outcome = Failed(error_type="DeliveryShapeError", message=str(exc))
    except Exception as exc:  # noqa: BLE001 — the no-raise contract: every raise maps shim-side
        outcome = Failed(error_type=type(exc).__name__, message=str(exc))
    finally:
        _CURRENT_RUN.reset(run_token)
    # Every terminal path leaves a durable trace (slice-1 invariant 3, lifted to
    # the vocabulary level); the fourth-row args key is the deterministic call
    # repr for v1 (value-type args; the typed args-digest rides the authoring tranche).
    from shepherd_dialect.trace import append_run_trace, build_run_trace_revision

    # The typed fourth-row args key (W3b, S1 seam 4 — ratified): same values =>
    # same cross-run key regardless of call spelling; unserializable arguments
    # fall back to the v1 call-repr rather than failing the trace append.
    try:
        from shepherd_dialect.task_meta import dump_task_args

        call_args = dump_task_args(fn, args, kwargs)
    except Exception:  # noqa: BLE001 — any serde failure means "no typed key", never "no trace"
        call_args = {"call": repr((args, tuple(sorted(kwargs.items()))))}

    retained = isinstance(outcome, Finished) and sealed_execution is not None
    merged = isinstance(outcome, Finished) and sealed_execution is None
    output_world = sealed_execution.handoff.output_world_oid if retained else ws._mg.world_oid() if merged else None
    trace_payload: dict[str, Any] | None = None
    if setup_failure:
        run_ctx.safe_point = "setup_failed"
        terminal = "failed"
        head = None
    elif launch_refused:
        run_ctx.safe_point = "refused"
        terminal = "refused"
        head = None
    else:
        if retained:
            terminal = "retained"
        elif merged:
            terminal = "merged"
        elif violation is not None and violation.phase == "precondition":
            terminal = "refused"  # no fork ever happened (S1 seam 3)
        else:
            terminal = "discarded"
        run_ctx.safe_point = terminal
        trace_payload = build_run_trace_revision(
            run_ref=ref.id,
            trace_owner_id=f"task:{task_id}:{ref.id}",
            frontier_id=f"frontier:{ref.id}",
            task_id=task_id,
            args=call_args,
            may_profile=may_resolution.resolved,
            may_source=may_resolution.source,
            terminal_status=terminal,
            input_world_oid=input_world,
            output_world_oid=output_world,
            operation_id=runtime_operation_id,
            extra_events=[
                *run_ctx.step_events,
                *(
                    [
                        {
                            "kind": "check.violation",
                            "check": violation.check.message or "Check",
                            "field": violation.field_name,
                            "phase": violation.phase,
                        }
                    ]
                    if violation is not None
                    else []
                ),
            ],
        )
        if parent_ctx is not None:
            head = f"memory-trace:{ref.id}"
        else:
            head = append_run_trace(ws._mg, trace_payload, scope=run_scope)
    output_citations = {}
    publication_error: dict[str, object] | None = None
    trace_ref = _trace_ref_for_nucleus_run(task_id=task_id, run_ref=ref.id) if head is not None else None
    if retained and trace_ref is not None:
        try:
            output_citations = _publish_nucleus_run_output_descriptors(
                ws,
                trace_ref=trace_ref,
                sealed_execution=sealed_execution,
            )
        except Exception as exc:  # noqa: BLE001 — custody exists; publish a diagnostic terminal row
            outcome = Failed(error_type=type(exc).__name__, message=str(exc))
            publication_error = _output_publication_error(outcome, sealed_execution=sealed_execution)
    error = None if publication_error is not None else _run_record_error(outcome)
    if setup_failure and error is not None:
        error = {**error, "stage": "setup", "phase": "admission"}
    if not launch_refused and parent_ctx is None:
        _publish_nucleus_run_record(
            ws,
            run_ctx=run_ctx,
            task_id=task_id,
            call_args=call_args,
            may_profile=may_resolution.resolved,
            status="retained" if retained else "merged" if merged else "failed",
            input_world_oid=input_world,
            output_world_oid=output_world,
            trace_head=head,
            trace_ref=trace_ref,
            runtime_operation_id=runtime_operation_id,
            started_at=started_at,
            error=error,
            outputs=output_citations,
            terminalization=_nucleus_run_terminalization(
                outcome,
                terminal=terminal,
                setup_failure=setup_failure,
                retained=retained,
                merged=merged,
                sealed_execution=sealed_execution,
                output_citations=output_citations,
                publication_error=publication_error,
            ),
            scope=run_scope,
        )
        run_record_published = True
    if parent_ctx is not None:
        parent_ctx.step_events.append(
            _child_run_event(
                parent_ctx=parent_ctx,
                run_ctx=run_ctx,
                task_id=task_id,
                may_profile=may_resolution.resolved,
                terminal=terminal,
                trace_head=head,
                runtime_operation_id=runtime_operation_id,
                runtime_scope_ref=runtime_scope_ref,
                run_record_published=run_record_published,
                outcome=outcome,
            )
        )
    return Run(
        outcome=outcome,
        ref=ref,
        duration=time.monotonic() - started,
        artifacts=tuple(run_ctx.artifacts),
        _trace_head=head,
        _trace_payload=trace_payload if head is not None and head.startswith("memory-trace:") else None,
    )


def _trace_ref_for_nucleus_run(*, task_id: str, run_ref: str) -> Any:
    from shepherd_dialect.workspace_control.schemas import TraceRef

    return TraceRef(
        run_id=run_ref,
        execution_id=f"task:{task_id}:{run_ref}",
        frontier_id=f"frontier:{run_ref}",
    )


def _publish_nucleus_run_output_descriptors(
    ws: Workspace,
    *,
    trace_ref: Any,
    sealed_execution: Any,
) -> dict[str, Any]:
    from shepherd_dialect.workspace_control.output_publication import publish_run_output_descriptor
    from shepherd_dialect.workspace_control.outputs import run_output_publication_from_seal_handoff

    draft = run_output_publication_from_seal_handoff(
        sealed_execution.handoff,
        parent=sealed_execution.seal_result.parent,
        trace_ref=trace_ref,
    )
    return {draft.output_name: publish_run_output_descriptor(ws.trace_store_path, draft)}


def _run_record_error(outcome: Finished[Any] | Exhausted | Stopped | Failed) -> dict[str, object] | None:
    if isinstance(outcome, Finished):
        return None
    if isinstance(outcome, Exhausted):
        return {"type": "BudgetExhausted", "message": outcome.reason}
    if isinstance(outcome, Stopped):
        return {"type": "Stopped", "message": outcome.reason}
    return {"type": outcome.error_type, "message": outcome.message}


def _output_publication_error(outcome: Failed, *, sealed_execution: Any) -> dict[str, object]:
    handoff = sealed_execution.handoff
    return {
        "type": outcome.error_type,
        "message": outcome.message,
        "stage": "output_publication",
        "phase": "run_output_descriptor",
        "retained_custody_ref": handoff.handoff_ref,
        "retained_output_world_oid": handoff.output_world_oid,
    }


def _nucleus_run_terminalization(
    outcome: Finished[Any] | Exhausted | Stopped | Failed,
    *,
    terminal: str,
    setup_failure: bool,
    retained: bool,
    merged: bool,
    sealed_execution: Any | None,
    output_citations: dict[str, Any],
    publication_error: dict[str, object] | None,
) -> Any:
    from shepherd_dialect.workspace_control.schemas import RunRetainedCustody, RunTerminalization

    if retained:
        if sealed_execution is None:
            raise RuntimeError("retained nucleus run has no sealed execution outcome")
        handoff = sealed_execution.handoff
        custody = RunRetainedCustody.from_seal_handoff(handoff)
        if publication_error is not None:
            return RunTerminalization(
                body_status="completed",
                world_disposition="retained",
                output_publication_status="failed",
                retained_custody=custody,
                publication_error=publication_error,
            )
        if output_citations:
            return RunTerminalization(
                body_status="completed",
                world_disposition="retained",
                output_publication_status="published",
                retained_custody=custody,
            )
        return RunTerminalization(
            body_status="completed",
            world_disposition="retained",
            output_publication_status="pending",
            retained_custody=custody,
        )
    if merged:
        return RunTerminalization(
            body_status="completed",
            world_disposition="merged",
            output_publication_status="not_applicable",
        )
    if terminal == "refused":
        return RunTerminalization(
            body_status="refused",
            world_disposition="none",
            output_publication_status="not_applicable",
        )
    if isinstance(outcome, Exhausted):
        body_status = "exhausted"
    elif isinstance(outcome, Stopped):
        body_status = "stopped"
    else:
        body_status = "failed"
    return RunTerminalization(
        body_status=body_status,
        world_disposition="none" if setup_failure else "discarded",
        output_publication_status="not_applicable",
    )


def _child_run_event(
    *,
    parent_ctx: _RunContext,
    run_ctx: _RunContext,
    task_id: str,
    may_profile: str,
    terminal: str,
    trace_head: str | None,
    runtime_operation_id: str | None,
    runtime_scope_ref: str | None,
    run_record_published: bool,
    outcome: Finished[Any] | Exhausted | Stopped | Failed,
) -> dict[str, Any]:
    if trace_head is None or runtime_operation_id is None or runtime_scope_ref is None:
        from shepherd_dialect.trace import CHILD_LAUNCH_REFUSED

        return {
            "id": f"child-launch-refused:{run_ctx.ref.id}",
            "kind": CHILD_LAUNCH_REFUSED,
            "parent_run_ref": parent_ctx.ref.id,
            "child_run_ref": run_ctx.ref.id,
            "child_task_id": task_id,
            "terminal_status": terminal,
            "may_profile": may_profile,
            "caused_by": f"task-call:{run_ctx.ref.id}",
            "reason": type(outcome).__name__,
        }
    trace_materialized = not trace_head.startswith("memory-trace:")
    if not trace_materialized or not run_record_published:
        from shepherd_dialect.trace import CHILD_VALUE_COMPLETED

        return {
            "id": f"child-value-completed:{run_ctx.ref.id}",
            "kind": CHILD_VALUE_COMPLETED,
            "parent_run_ref": parent_ctx.ref.id,
            "child_run_ref": run_ctx.ref.id,
            "child_task_id": task_id,
            "child_trace_token": trace_head,
            "child_lifecycle": _child_lifecycle(outcome, terminal),
            "terminal_status": terminal,
            "may_profile": may_profile,
            "caused_by": f"task-call:{run_ctx.ref.id}",
            "evidence_level": "same_process_value",
            "trace_materialized": trace_materialized,
            "ledger_visible": run_record_published,
            "operation_identity_kind": "logical_placeholder",
        }
    from shepherd_dialect.trace import CHILD_RUN_COMPLETED

    return {
        "id": f"child-run-completed:{run_ctx.ref.id}",
        "kind": CHILD_RUN_COMPLETED,
        "parent_run_ref": parent_ctx.ref.id,
        "child_run_ref": run_ctx.ref.id,
        "child_task_id": task_id,
        "child_operation_id": runtime_operation_id,
        "child_logical_scope_ref": runtime_scope_ref,
        "child_execution_scope_ref": runtime_scope_ref,
        "child_trace_head": trace_head,
        "child_lifecycle": _child_lifecycle(outcome, terminal),
        "child_world_disposition": "release",
        "child_scope_terminal_status": "discarded",
        "terminal_status": terminal,
        "may_profile": may_profile,
        "caused_by": f"task-call:{run_ctx.ref.id}",
    }


def _child_lifecycle(outcome: Finished[Any] | Exhausted | Stopped | Failed, terminal: str) -> str:
    if isinstance(outcome, Finished):
        return "finished"
    if terminal == "refused":
        return "refused"
    return "failed"


def _publish_nucleus_run_record(
    ws: Workspace,
    *,
    run_ctx: _RunContext,
    task_id: str,
    call_args: dict[str, Any],
    may_profile: str,
    status: str,
    input_world_oid: str | None,
    output_world_oid: str | None,
    trace_head: str | None,
    trace_ref: Any | None,
    runtime_operation_id: str | None,
    started_at: str,
    error: dict[str, object] | None,
    outputs: dict[str, Any] | None,
    terminalization: Any,
    scope: Any,
) -> None:
    from shepherd_dialect.workspace_control.run_ledger import (
        canonical_digest,
        publish_terminal_run_record,
        utc_now,
    )
    from shepherd_dialect.workspace_control.schemas import RunLaunchContext, RunOperationRefs, RunRecord

    record = RunRecord(
        run_ref=run_ctx.ref.id,
        task_id=task_id,
        task_version="nucleus",
        task_schema_digest=canonical_digest({"task_id": task_id, "task_version": "nucleus"}),
        args_digest=canonical_digest({"args": call_args}),
        may_profile=may_profile,
        provider="shepherd.nucleus.v1",
        status=status,
        operation_refs=RunOperationRefs(
            runtime_operation=runtime_operation_id,
            trace_head=trace_head,
        ),
        trace_ref=trace_ref,
        input_workspace_world_oid=input_world_oid,
        terminal_workspace_world_oid=output_world_oid,
        outputs=outputs or {},
        started_at=started_at,
        finished_at=utc_now(),
        parent_run_ref=run_ctx.parent_ref,
        launch_context=RunLaunchContext(
            launch_surface="python",
            may_profile=may_profile,
            parent_run_ref=run_ctx.parent_ref,
        ),
        error=error,
        terminalization=terminalization,
    )
    publish_terminal_run_record(ws._mg, record, scope=scope)


class TaskCallable(Generic[T]):
    """The ``@task`` wrapper: plain calls unwrap; ``.detailed()`` never raises."""

    def __init__(self, fn: Any, *, may: str | None = None) -> None:
        self._fn = fn
        self._may = may
        if may is not None:
            self.may_default = may
            self.__shepherd_may_default__ = may
        self.__name__ = getattr(fn, "__name__", "task")
        self._is_async = inspect.iscoroutinefunction(fn)

    def _detailed_sync(
        self,
        args: tuple[Any, ...],
        kwargs: dict[str, Any],
        *,
        success_disposition: str = "merge",
    ) -> Run[T]:
        return _execute(self._fn, args, kwargs, may=self._may, success_disposition=success_disposition)

    def detailed(self, *args: Any, **kwargs: Any) -> Any:
        """Run and return the ``Run[T]`` (awaitable for async bodies); never raises."""
        if self._is_async:

            async def run() -> Run[T]:
                return self._detailed_sync(args, kwargs)

            return run()
        return self._detailed_sync(args, kwargs)

    def detailed_retained(self, *args: Any, **kwargs: Any) -> Any:
        """Run through the internal seal-mode spine and return a retained ``Run[T]``.

        This is a narrow integration surface for the nucleus/vcs-core
        retained-output path. It does not add task-level handle threading or
        best-of-N composition.
        """
        if self._is_async:

            async def run() -> Run[T]:
                return self._detailed_sync(args, kwargs, success_disposition="seal")

            return run()
        return self._detailed_sync(args, kwargs, success_disposition="seal")

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        if self._is_async:

            async def run() -> T:
                _ambient()  # WorkspaceNotConfigured raises before any work
                return self._detailed_sync(args, kwargs).unwrap()

            return run()
        _ambient()
        return self._detailed_sync(args, kwargs).unwrap()


def task(fn: Any | None = None, *, may: str | None = None) -> TaskCallable[Any] | Any:
    """Wrap a sync or async function as a callable task."""

    def wrap(actual: Any) -> TaskCallable[Any]:
        return TaskCallable(actual, may=may)

    if fn is None:
        return wrap
    return wrap(fn)
