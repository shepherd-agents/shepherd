"""Normative API for the `-lite` kernel: start_kernel_run,
resume_kernel_run, validate_observation_stream.

Per `260521-0600-kernel.md` §"API Shape" and 2026-05-24
§"Settled Design Decisions" entries (post-#72 design pass item D/E for
envelope shape; pre-#73 micro-design pass for KernelRejection fields)
and `260524-observation-stream-spike.md` (driver placement and shape).

All three functions return `KernelResultEnvelope` and compose over
existing replay primitives (`start_kernel_replay`, `resume_kernel_replay`)
plus the production admission validator
(`validate_admitted_observation` from `kernel/admission.py`).

`validate_observation_stream(...)` threads `KernelReplayState` through
sequential `resume_kernel_run(...)` calls with fail-fast semantics:
the first failing observation returns immediately with a
`KernelRejection(kind="observation-admission", rejection_index=...,
rejection_class=..., diagnostic=...)` payload.

Admission failure shape: `resume_kernel_run(...)` raises
`AdmittedObservationError` directly on bundle-level admission failure
(no new transition was constructed). Callers that need envelope-shaped
admission rejections should use `validate_observation_stream(...)`,
which catches the exception, wraps it as an envelope using the prior
frontier transition, and reports `rejection_index=i` for the failing
observation's stream position.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from shepherd_kernel_v3_reference.envelope import (
    CompletedResult,
    KernelRejection,
    KernelResultEnvelope,
)
from shepherd_kernel_v3_reference.kernel.admission import (
    AdmittedObservation,
    AdmittedObservationError,
    validate_admitted_observation,
)
from shepherd_kernel_v3_reference.kernel.replay import (
    ExternalEffectRequest,
    KernelReplayRejected,
    KernelReplayState,
    ReplayableCompleted,
    ReplayableKernelTransition,
    resume_kernel_replay,
    start_kernel_replay,
)

if TYPE_CHECKING:
    from shepherd_kernel_v3_reference.kernel.program_admission import (
        KernelProgramInput,
    )
    from shepherd_kernel_v3_reference.source.effects import EffectRegistry
    from shepherd_kernel_v3_reference.source.values import Env


def start_kernel_run(
    program: KernelProgramInput,
    env: Env | None = None,
    *,
    registry: EffectRegistry | None = None,
) -> tuple[KernelReplayState, KernelResultEnvelope]:
    """Start a new kernel run and return `(state, envelope)`.

    The envelope wraps the first transition: `status='completed'` if the
    program completes immediately, `status='external-effect-request'` if
    it suspends on an unhandled effect.

    Profile is read from the prepared artifact's lineage; no profile=
    argument per 2026-05-22 §"Profile attachment on PreparedKernelProgram".
    """

    state, transition = start_kernel_replay(program, env=env, registry=registry)
    envelope = _envelope_for(state, transition)
    return state, envelope


def resume_kernel_run(
    state: KernelReplayState,
    observation: AdmittedObservation,
) -> tuple[KernelReplayState, KernelResultEnvelope]:
    """Validate the observation bundle and resume the kernel run.

    Validates `observation` against `state` via
    `validate_admitted_observation(...)` (#73b). On admission failure,
    **raises `AdmittedObservationError`** — admission failure produces no
    new transition, so an envelope cannot be constructed cleanly at this
    layer. Callers that need an envelope-shaped admission rejection
    should use `validate_observation_stream(...)` which wraps the
    exception with the prior frontier transition.

    On admission success, delegates to `resume_kernel_replay(...)`.
    `KernelReplayRejected` from resume time is unwrapped into a
    `KernelRejection(kind='execution-failure', ...)` envelope with the
    underlying cause's message preserved per
    `260524-observation-stream-spike.md` §"What Was Surprising" #2.
    """

    validate_admitted_observation(observation, state)

    try:
        new_state, transition = resume_kernel_replay(
            state,
            observation.request,
            observation.observation,
        )
    except KernelReplayRejected as exc:
        cause = getattr(exc, "reason", None) or exc.__cause__ or exc
        rejection = KernelRejection(
            kind="execution-failure",
            diagnostic=str(cause),
            program_ref=state.program_ref,
            partial_records=exc.transition.trace_delta,
        )
        envelope = KernelResultEnvelope(
            profile=state.profile,
            status="rejected",
            payload=rejection,
            transition=exc.transition,
        )
        return exc.state, envelope

    return new_state, _envelope_for(new_state, transition)


def validate_observation_stream(
    program: KernelProgramInput,
    observations: tuple[AdmittedObservation, ...],
    *,
    env: Env | None = None,
    registry: EffectRegistry | None = None,
) -> tuple[KernelReplayState, KernelResultEnvelope]:
    """Thread state through sequential resume_kernel_run with fail-fast.

    Starts a fresh run with `start_kernel_run(...)`, then folds each
    observation in turn through `resume_kernel_run(...)`. Returns the
    final `(state, envelope)`.

    Fail-fast: if any observation fails admission or resume execution,
    returns immediately with the rejection envelope (rejection_index=i
    for the failing observation's stream position); subsequent
    observations are not evaluated.

    Stream-too-long is detected after the program terminates: if more
    observations remain, returns a rejection envelope with
    rejection_class='state-level' at the surplus index.

    Per `260524-observation-stream-spike.md` §"Placement": this function
    bundles with `start_kernel_run` / `resume_kernel_run` in the
    normative API surface.
    """

    state, envelope = start_kernel_run(program, env=env, registry=registry)

    for i, observation in enumerate(observations):
        # Stream-too-long detection (state became terminal/rejected)
        if state.terminal:
            rejection = KernelRejection(
                kind="observation-admission",
                diagnostic="stream has more observations than program needs "
                           "(state is terminal)",
                program_ref=state.program_ref,
                rejection_index=i,
                rejection_class="state-level",
            )
            return state, KernelResultEnvelope(
                profile=state.profile,
                status="rejected",
                payload=rejection,
                transition=envelope.transition,
            )
        if state.rejected:
            rejection = KernelRejection(
                kind="observation-admission",
                diagnostic="state is rejected; cannot admit further observations",
                program_ref=state.program_ref,
                rejection_index=i,
                rejection_class="state-level",
            )
            return state, KernelResultEnvelope(
                profile=state.profile,
                status="rejected",
                payload=rejection,
                transition=envelope.transition,
            )

        # Capture the prior transition before resume_kernel_run; we need it
        # to construct the rejection envelope if admission fails (no new
        # transition is constructed in that case).
        prior_transition = envelope.transition

        try:
            state, envelope = resume_kernel_run(state, observation)
        except AdmittedObservationError as exc:
            rejection = KernelRejection(
                kind="observation-admission",
                diagnostic=str(exc),
                program_ref=state.program_ref,
                rejection_index=i,
                rejection_class=exc.rejection_class,
            )
            return state, KernelResultEnvelope(
                profile=state.profile,
                status="rejected",
                payload=rejection,
                transition=prior_transition,
            )

        if envelope.status == "rejected":
            # resume_kernel_run already constructed an execution-failure
            # envelope. Re-tag rejection_index if it's observation-admission
            # (which currently it isn't from resume_kernel_run, but defensive
            # for future).
            return state, envelope

    return state, envelope


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _envelope_for(
    state: KernelReplayState,
    transition: ReplayableKernelTransition,
) -> KernelResultEnvelope:
    """Construct a KernelResultEnvelope for a start/resume result."""

    payload = transition.payload
    if isinstance(payload, ReplayableCompleted):
        result = CompletedResult(
            program_ref=transition.program_ref,
            value=payload.value,
        )
        return KernelResultEnvelope(
            profile=state.profile,
            status="completed",
            payload=result,
            transition=transition,
        )
    if isinstance(payload, ExternalEffectRequest):
        return KernelResultEnvelope(
            profile=state.profile,
            status="external-effect-request",
            payload=payload,
            transition=transition,
        )
    # ReplayableRejected → envelope status='rejected' with execution-failure
    rejection = KernelRejection(
        kind="execution-failure",
        diagnostic=getattr(payload, "reason_message", "kernel rejected transition"),
        program_ref=transition.program_ref,
        partial_records=transition.trace_delta,
    )
    return KernelResultEnvelope(
        profile=state.profile,
        status="rejected",
        payload=rejection,
        transition=transition,
    )


__all__ = [
    "resume_kernel_run",
    "start_kernel_run",
    "validate_observation_stream",
]
