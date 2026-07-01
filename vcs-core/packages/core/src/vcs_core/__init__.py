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
    InvalidRepositoryStateError,
    LifecycleRecoveryRequiredError,
    MergePreconditionError,
    OpenScopeError,
    OrphanedOperationsError,
    OverlayDirtyError,
    ParentWorkingTreeDivergedError,
    RefResolutionError,
    StaleScopeError,
    SubstrateNotBoundError,
    UnscopedMutationError,
    VerifyFailedError,
    WorldQuiescenceError,
)
from vcs_core._substrate_runtime import build_builtin_substrate_context

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
    "ActivationError",
    "AuthorityAspect",
    "BindingConfig",
    "BoundSubstrate",
    "CommitInfo",
    "DeclarativeFilesystemSubstrate",
    "DiffSummary",
    "DirtyPushError",
    "EffectRecord",
    "FileChange",
    "FilesystemSubstrate",
    "GitSubstrate",
    "InterruptedLifecycleError",
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
    "ScopeInfo",
    "ScopeStack",
    "SealCandidateHandoff",
    "SealResult",
    "SealedExecutionOutcome",
    "SecretRef",
    "SelectedBindingRevision",
    "StaleScopeError",
    "Status",
    "Store",
    "SubstrateAuthority",
    "SubstrateManifest",
    "SubstrateNotBoundError",
    "SubstratePlugin",
    "UnscopedMutationError",
    "VcsCore",
    "VcsCoreConfig",
    "VerifyFailedError",
    "WorkspaceChange",
    "WorldQuiescenceError",
    "build_builtin_substrate_context",
]
