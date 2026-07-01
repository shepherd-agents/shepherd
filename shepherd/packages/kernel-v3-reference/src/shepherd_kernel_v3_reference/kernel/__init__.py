"""Public runtime construction API for the v3 kernel fragment."""

from typing import TYPE_CHECKING

from shepherd_kernel_v3_reference.kernel.elaborate import (
    Elaborator,
    KernelProgram,
    elaborate,
    elaborate_publication_experimental,
)
from shepherd_kernel_v3_reference.kernel.machine import run_kernel
from shepherd_kernel_v3_reference.kernel.program_admission import (
    PreparedKernelProgram,
    admit_and_prepare,
    prepare_kernel_program,
)

if TYPE_CHECKING:
    from shepherd_kernel_v3_reference.kernel.replay import (
        CONTINUATION_REPLAY_ARTIFACT_SCHEMA_VERSION,
        CONTINUATION_SOURCE_KEY_SCHEMA_VERSION,
        EXTERNAL_EFFECT_REQUEST_SCHEMA_VERSION,
        HOST_COMPLETED_SCHEMA_VERSION,
        KERNEL_REPLAY_JOURNAL_SCHEMA_VERSION,
        KERNEL_REPLAY_STATE_SCHEMA_VERSION,
        REPLAYABLE_KERNEL_TRANSITION_SCHEMA_VERSION,
        ContinuationReplayArtifact,
        ContinuationReplayError,
        ContinuationReplayLedger,
        ContinuationReplaySerializationError,
        ExternalEffectRequest,
        ExternalEffectRequestDescriptor,
        ExternalEffectRequestRef,
        HostCompleted,
        KernelReplayJournal,
        KernelReplayRejected,
        KernelReplaySession,
        KernelReplayState,
        OpenReplayRequest,
        ReplayableCompleted,
        ReplayableExternalEffectRequest,
        ReplayableKernelResult,
        ReplayableKernelTransition,
        ReplayableRejected,
        ReplayArtifactCatalog,
        continuation_replay_artifact_from_json,
        continuation_replay_artifact_from_objects,
        continuation_replay_artifact_to_json,
        external_effect_request_from_json,
        external_effect_request_to_json,
        host_completed_from_json,
        host_completed_to_json,
        kernel_replay_journal_current_request,
        kernel_replay_journal_current_request_descriptor,
        kernel_replay_journal_from_json,
        kernel_replay_journal_to_json,
        kernel_replay_state_from_journal,
        kernel_replay_state_from_json,
        kernel_replay_state_to_json,
        replayable_kernel_transition_from_json,
        replayable_kernel_transition_to_json,
        resume_continuation,
        resume_external_effect_request,
        resume_kernel_replay,
        resume_kernel_replay_from_journal,
        resume_replayable_kernel_transition,
        start_kernel_replay,
        start_replayable_kernel_run,
        start_replayable_kernel_transition,
    )
from shepherd_kernel_v3_reference.kernel.validate import KernelProgramValidationError, validate_kernel_program

__all__ = [
    "CONTINUATION_REPLAY_ARTIFACT_SCHEMA_VERSION",
    "CONTINUATION_SOURCE_KEY_SCHEMA_VERSION",
    "EXTERNAL_EFFECT_REQUEST_SCHEMA_VERSION",
    "HOST_COMPLETED_SCHEMA_VERSION",
    "KERNEL_REPLAY_JOURNAL_SCHEMA_VERSION",
    "KERNEL_REPLAY_STATE_SCHEMA_VERSION",
    "REPLAYABLE_KERNEL_TRANSITION_SCHEMA_VERSION",
    "ContinuationReplayArtifact",
    "ContinuationReplayError",
    "ContinuationReplayLedger",
    "ContinuationReplaySerializationError",
    "Elaborator",
    "ExternalEffectRequest",
    "ExternalEffectRequestDescriptor",
    "ExternalEffectRequestRef",
    "HostCompleted",
    "KernelProgram",
    "KernelProgramValidationError",
    "KernelReplayJournal",
    "KernelReplayRejected",
    "KernelReplaySession",
    "KernelReplayState",
    "OpenReplayRequest",
    "PreparedKernelProgram",
    "ReplayArtifactCatalog",
    "ReplayableCompleted",
    "ReplayableExternalEffectRequest",
    "ReplayableKernelResult",
    "ReplayableKernelTransition",
    "ReplayableRejected",
    "admit_and_prepare",
    "continuation_replay_artifact_from_json",
    "continuation_replay_artifact_from_objects",
    "continuation_replay_artifact_to_json",
    "elaborate",
    "elaborate_publication_experimental",
    "external_effect_request_from_json",
    "external_effect_request_to_json",
    "host_completed_from_json",
    "host_completed_to_json",
    "kernel_replay_journal_current_request",
    "kernel_replay_journal_current_request_descriptor",
    "kernel_replay_journal_from_json",
    "kernel_replay_journal_to_json",
    "kernel_replay_state_from_journal",
    "kernel_replay_state_from_json",
    "kernel_replay_state_to_json",
    "prepare_kernel_program",
    "replayable_kernel_transition_from_json",
    "replayable_kernel_transition_to_json",
    "resume_continuation",
    "resume_external_effect_request",
    "resume_kernel_replay",
    "resume_kernel_replay_from_journal",
    "resume_replayable_kernel_transition",
    "run_kernel",
    "start_kernel_replay",
    "start_replayable_kernel_run",
    "start_replayable_kernel_transition",
    "validate_kernel_program",
]


def __getattr__(name: str) -> object:
    """Lazily resolve replay re-exports to avoid a trace<->kernel import cycle.

    kernel.replay imports trace.machine, which imports kernel.continuation_objects
    (re-entering this package). Importing replay eagerly here deadlocks when the
    trace subpackage is imported before the kernel package (e.g. shepherd_runtime).
    """
    import importlib

    _replay = importlib.import_module(f"{__name__}.replay")
    try:
        return getattr(_replay, name)
    except AttributeError:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}") from None
