"""vcs-core: Provenance-native version control for executable worlds.

A transactional execution infrastructure built on bare Git repositories
via pygit2. Store is the semantic layer; VcsCore coordinates substrates.
"""

from __future__ import annotations

__version__ = "0.1.0a1"

# --- Core classes ---
# --- Errors ---
from vcs_core._errors import (
    ActivationError,
    DirtyPushError,
    InterruptedLifecycleError,
    InvalidIdentityError,
    InvalidRepositoryStateError,
    LifecycleRecoveryRequiredError,
    MergePreconditionError,
    OpenScopeError,
    OrphanedOperationsError,
    OverlayDirtyError,
    ParentWorkingTreeDivergedError,
    ReadOnlyCarrierError,
    RefResolutionError,
    ScopeAdmissionError,
    SiblingGroupRecoveryRequiredError,
    StaleScopeError,
    SubstrateCommandError,
    SubstrateNotBoundError,
    UnknownForkHintError,
    UnresolvedPatchPathError,
    UnscopedMutationError,
    UnsupportedOverlayEntryError,
    VcsCoreError,
    VerifyFailedError,
    WorkspaceAuthorityRecoveryRequiredError,
    WorldQuiescenceError,
)
from vcs_core._signals import terminate_as_interrupt
from vcs_core._substrate_runtime import build_builtin_substrate_context

# --- World-value vocabulary (product types; promoted from private modules,
# 260704-1410-plan.md V1.1 step 2). Re-exports: the public names are stable even
# if the implementation home later moves (e.g. canonical_digest/canonical_bytes
# consolidating into commons_vcs.canonical, P4 rider 2). Digest contract:
# byte-identical output under the same content-addressing domain — moving the
# home must not change the bytes. EvidenceRef / WORLD_TRANSITION_SCHEMA feed the
# durable-schema design review, which inherits them as public consciously. ---
from vcs_core._transition_kernel_records import EvidenceRef
from vcs_core._world_types import (
    WORLD_TRANSITION_SCHEMA,
    WorldSnapshot,
    canonical_bytes,
    canonical_digest,
)

# --- Config (public in R1b) ---
from vcs_core.authority import AuthorityAspect, SubstrateAuthority
from vcs_core.config import BindingConfig, SecretRef, VcsCoreConfig
from vcs_core.git_substrate import GitSubstrate
from vcs_core.manifest import SubstrateManifest, SubstratePlugin
from vcs_core.scope_stack import ScopeStack
from vcs_core.sqlite_substrate import SQLiteSubstrate
from vcs_core.store import Store

# --- Built-in substrates ---
from vcs_core.substrates import DeclarativeFilesystemSubstrate, FilesystemSubstrate, MarkerSubstrate

# --- DTOs ---
from vcs_core.types import (
    BoundSubstrate,
    CommitInfo,
    DiffSummary,
    EffectRecord,
    FileChange,
    MaterializationPhase,
    MaterializationPlan,
    OperationHistory,
    OperationSummary,
    RebaseResult,
    RecordedCommandOutcome,
    RecoverySnapshot,
    RetainedOutputIdentity,
    RetainedOutputQueryResult,
    RetainedOutputSelectionResult,
    RetainedOutputSettlement,
    RetainedOutputSettlementResult,
    RetainedWorkspaceHandle,
    ScopeInfo,
    SealCandidateHandoff,
    SealedExecutionOutcome,
    SealResult,
    SelectedBindingRevision,
    Status,
    WorkspaceChange,
)
from vcs_core.vcscore import VcsCore

__all__ = [
    "WORLD_TRANSITION_SCHEMA",
    "ActivationError",
    "AuthorityAspect",
    "BindingConfig",
    "BoundSubstrate",
    "CommitInfo",
    "DeclarativeFilesystemSubstrate",
    "DiffSummary",
    "DirtyPushError",
    "EffectRecord",
    "EvidenceRef",
    "FileChange",
    "FilesystemSubstrate",
    "GitSubstrate",
    "InterruptedLifecycleError",
    "InvalidIdentityError",
    "InvalidRepositoryStateError",
    "LifecycleRecoveryRequiredError",
    "MarkerSubstrate",
    "MaterializationPhase",
    "MaterializationPlan",
    "MergePreconditionError",
    "OpenScopeError",
    "OperationHistory",
    "OperationSummary",
    "OrphanedOperationsError",
    "OverlayDirtyError",
    "ParentWorkingTreeDivergedError",
    "ReadOnlyCarrierError",
    "RebaseResult",
    "RecordedCommandOutcome",
    "RecoverySnapshot",
    "RefResolutionError",
    "RetainedOutputIdentity",
    "RetainedOutputQueryResult",
    "RetainedOutputSelectionResult",
    "RetainedOutputSettlement",
    "RetainedOutputSettlementResult",
    "RetainedWorkspaceHandle",
    "SQLiteSubstrate",
    "ScopeAdmissionError",
    "ScopeInfo",
    "ScopeStack",
    "SealCandidateHandoff",
    "SealResult",
    "SealedExecutionOutcome",
    "SecretRef",
    "SelectedBindingRevision",
    "SiblingGroupRecoveryRequiredError",
    "StaleScopeError",
    "Status",
    "Store",
    "SubstrateAuthority",
    "SubstrateCommandError",
    "SubstrateManifest",
    "SubstrateNotBoundError",
    "SubstratePlugin",
    "UnknownForkHintError",
    "UnresolvedPatchPathError",
    "UnscopedMutationError",
    "UnsupportedOverlayEntryError",
    "VcsCore",
    "VcsCoreConfig",
    "VcsCoreError",
    "VerifyFailedError",
    "WorkspaceAuthorityRecoveryRequiredError",
    "WorkspaceChange",
    "WorldQuiescenceError",
    "WorldSnapshot",
    "build_builtin_substrate_context",
    "canonical_bytes",
    "canonical_digest",
    "terminate_as_interrupt",
]
