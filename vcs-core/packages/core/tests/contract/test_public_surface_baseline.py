"""Baseline contract checks for the current public and documented surface."""

from __future__ import annotations

import importlib
import re
from pathlib import Path

import vcs_core as vcs_core_pkg
from vcs_core.spi import __all__ as spi_all

from ..support.public_surface import PUBLIC_LOOKING_TOP_LEVEL_MODULES

EXPECTED_PACKAGE_ROOT_EXPORTS = {
    "ActivationError",
    "AuthorityAspect",
    "RetainedOutputIdentity",
    "BindingConfig",
    "build_builtin_substrate_context",
    "BoundSubstrate",
    "CommitInfo",
    "DeclarativeFilesystemSubstrate",
    "DiffSummary",
    "DirtyPushError",
    "EffectRecord",
    "FileChange",
    "FilesystemSubstrate",
    "GitSubstrate",
    "InvalidRepositoryStateError",
    "InterruptedLifecycleError",
    "LifecycleRecoveryRequiredError",
    "MarkerSubstrate",
    "MaterializationPhase",
    "MaterializationPlan",
    "MergePreconditionError",
    "VcsCore",
    "VcsCoreConfig",
    "OpenScopeError",
    "OperationHistory",
    "OperationSummary",
    "OrphanedOperationsError",
    "OverlayDirtyError",
    "ParentWorkingTreeDivergedError",
    "RebaseResult",
    "RefResolutionError",
    "RecordedCommandOutcome",
    "RecoverySnapshot",
    "RetainedOutputQueryResult",
    "RetainedOutputSelectionResult",
    "RetainedOutputSettlement",
    "RetainedOutputSettlementResult",
    "RetainedWorkspaceHandle",
    "ScopeInfo",
    "ScopeStack",
    "SealCandidateHandoff",
    "SealedExecutionOutcome",
    "SealResult",
    "SecretRef",
    "SelectedBindingRevision",
    "SQLiteSubstrate",
    "StaleScopeError",
    "Status",
    "Store",
    "SubstrateAuthority",
    "SubstrateManifest",
    "SubstrateNotBoundError",
    "SubstratePlugin",
    "UnscopedMutationError",
    "VerifyFailedError",
    "WorldQuiescenceError",
    "WorkspaceChange",
}

# The stable implement-side surface (decisions.md `spi-top-level-promotion`).
# `vcs_core.spi` is the single source of truth.
EXPECTED_SPI_EXPORTS = {
    # Surface additions not previously baseline-tracked (Lane C authority +
    # keyed-json/revision-storage substrate work); synced 2026-07-03.
    "AuthorityRole",
    "CrashLagOrdering",
    "DriverAuthorityRequiredError",
    "GrowthBound",
    "KeyedJsonPut",
    "KeyedJsonTreeDraft",
    "ReadSafety",
    "RevisionContentDraft",
    "RevisionStorageProfile",
    "RevisionStorageShape",
    # Versioning
    "SPI_VERSION",
    "SUBSTRATE_DRIVER_CONTRACT_REVISION",
    # Execution-mechanism capability surface (opt-in; separately versioned)
    "EXECUTION_CAPABILITY_VERSION",
    "ConfinementSpec",
    "ExecutionAuthorityRequired",
    "ExecutionBoundDriver",
    "ExecutionCapability",
    "NetMode",
    "NetworkPolicy",
    "UnsupportedConfinementSpecError",
    "verify_execution_negotiation",
    # Driver protocol + mixin
    "SubstrateDriver",
    "BaseSubstrateDriver",
    "command",
    # Typed ingress request family
    "IngressRequest",
    "CommandRequest",
    "ScanRequest",
    "CaptureRequest",
    "ReduceRequest",
    "MergeRequest",
    # Capability + surface
    "CapabilitySet",
    "ActiveSurface",
    # Context + child-world resolution
    "DriverContext",
    "ChildWorldResolver",
    "ChildWorldSnapshot",
    # Result + draft DTOs
    "DriverIngressResult",
    "TransitionDraft",
    "ObservationDraft",
    "RetentionHint",
    "DriverSelectionRequirementDraft",
    "Diagnostic",
    # Introspection schema (driver-side; natural names)
    "DriverSchema",
    "CommandSpec",
    "ParamSpec",
    "ScanSpec",
    "MergeSpec",
    "CaptureAdapterSchema",
    # Capture family
    "CaptureAdapter",
    "CaptureAdapterRegistry",
    "ObservationSink",
    "TupleSink",
    "FanOutSink",
    "SinkFailure",
    "ParseResult",
    # Errors
    "SubstrateContractError",
    "UnsupportedRequestError",
    "CapabilityContractViolation",
    "EvidenceKindReconciliationError",
    "SurfacePolicyError",
    # Validators
    "validate_driver_ingress",
    "validate_driver_ingress_result",
    # Support types
    "PayloadDescriptorClaim",
    "RelationshipRequirement",
    "SubstrateStoreIdentity",
}

EXPECTED_INTERNAL_HELPER_MODULE = "vcs_core._substrate_runtime"
EXPECTED_PUBLIC_BUILTIN_HELPER_IMPORT = "build_builtin_substrate_context"
EXPECTED_GUIDE_TIME_PUBLIC_BUILTIN_IMPORT_GUIDES = [
    "GUIDE-integration.md",
    "GUIDE-store-first.md",
]
PUBLIC_HELPER_IMPORT_PATTERN = re.compile(
    r"from\s+vcs_core\s+import\s+(?:\([^)]*\bbuild_builtin_substrate_context\b[^)]*\)|[^\n]*\bbuild_builtin_substrate_context\b[^\n]*)",
    re.MULTILINE | re.DOTALL,
)
INTERNAL_HELPER_IMPORT_PATTERN = re.compile(
    r"^\s*(?:from\s+vcs_core\._substrate_runtime\s+import\s+[^\n]+|import\s+vcs_core\._substrate_runtime(?:\s+as\s+\w+)?)\s*$",
    re.MULTILINE,
)


def _vcscore_root() -> Path:
    return Path(__file__).resolve().parents[4]


def _public_looking_top_level_modules() -> set[str]:
    package_root = _vcscore_root() / "packages" / "core" / "src" / "vcs_core"
    modules: set[str] = set()
    for entry in package_root.iterdir():
        if entry.name in {"__init__.py", "__pycache__", "py.typed"} or entry.name.startswith("_"):
            continue
        if entry.is_file() and entry.suffix == ".py":
            modules.add(entry.stem)
            continue
        if entry.is_dir() and (entry / "__init__.py").is_file():
            modules.add(entry.name)
    return modules


def test_package_root_exports_current_baseline_inventory() -> None:
    assert set(vcs_core_pkg.__all__) == EXPECTED_PACKAGE_ROOT_EXPORTS
    assert len(vcs_core_pkg.__all__) == len(EXPECTED_PACKAGE_ROOT_EXPORTS)


def test_spi_namespace_exports_current_baseline_inventory() -> None:
    assert set(spi_all) == EXPECTED_SPI_EXPORTS
    assert len(spi_all) == len(EXPECTED_SPI_EXPORTS)


def test_public_looking_top_level_modules_current_baseline_inventory() -> None:
    assert _public_looking_top_level_modules() == PUBLIC_LOOKING_TOP_LEVEL_MODULES


def test_public_looking_top_level_modules_are_importable() -> None:
    for module_name in PUBLIC_LOOKING_TOP_LEVEL_MODULES:
        imported = importlib.import_module(f"vcs_core.{module_name}")
        assert imported.__name__ == f"vcs_core.{module_name}"


def test_only_allowlisted_guides_may_mention_internal_runtime_helper_module() -> None:
    guides_root = _vcscore_root() / "design" / "guides"
    guides = sorted(guides_root.glob("GUIDE-*.md"))

    leaking_guides = sorted(
        guide.name for guide in guides if EXPECTED_INTERNAL_HELPER_MODULE in guide.read_text(encoding="utf-8")
    )

    assert leaking_guides == []


def test_boundary_guides_use_supported_public_built_in_helper() -> None:
    guides_root = _vcscore_root() / "design" / "guides"

    for guide_name in EXPECTED_GUIDE_TIME_PUBLIC_BUILTIN_IMPORT_GUIDES:
        guide = guides_root / guide_name
        text = guide.read_text(encoding="utf-8")
        assert EXPECTED_PUBLIC_BUILTIN_HELPER_IMPORT in text
        assert PUBLIC_HELPER_IMPORT_PATTERN.search(text)
        assert not INTERNAL_HELPER_IMPORT_PATTERN.findall(text)
