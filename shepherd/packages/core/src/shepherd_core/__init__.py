"""Shepherd kernel package.

`shepherd_core` owns the kernel primitives that other Shepherd packages build on:

- effect types, effect registry, and stream/fold primitives
- provider and execution-context protocols
- shared schema, strict-mode, and foundational utilities
- immutable scope substrate and related kernel types

Preferred public owner paths for non-kernel APIs are:

- `shepherd_runtime.scope` and `shepherd_runtime.lifecycle`
- `shepherd_runtime.task.*`
- `shepherd_runtime.step.api`
- `shepherd_runtime.combinators`
- `shepherd_runtime.handlers` and `shepherd_runtime.effect_materialization`
- `shepherd_export`
- `shepherd_transform`

Quick Start
-----------
    from shepherd_core import Effect, Stream
"""

from __future__ import annotations

__version__ = "0.2.0"

# =============================================================================
# Foundation Primitives (Layer 0)
# =============================================================================

# Configuration helpers (used by shepherd-contexts and runtime)
from shepherd_core._infer import Infer, _InferMarker

# Schema utilities (canonical name: type_to_json_schema)
from shepherd_core._shared.schema import type_to_json_schema

# =============================================================================
# Autoconfig (schema extraction for config inference)
# =============================================================================
from shepherd_core.autoconfig import (
    build_inference_model,
    extract_infer_fields,
)
from shepherd_core.config import is_strict_mode, set_strict_mode

# Layer 2: ExecutionContext Protocol
from shepherd_core.context import (
    ExecutionContext,
    ExecutionContextDefaults,
    compute_composite_reversibility,
    is_execution_context,
    is_reversible,
)

# =============================================================================
# Effects and Stream
# =============================================================================
from shepherd_core.effects import (
    # Registry
    EFFECT_TYPES,
    KERNEL_EFFECT_REGISTRY,
    AgentMessage,
    AgentThinking,
    ArtifactMissing,
    # Artifact
    ArtifactWritten,
    ContextCaptured,
    ContextCleanedUp,
    # Context lifecycle
    ContextConfigured,
    ContextPrepared,
    # Data types
    DiffPatch,
    # Base
    Effect,
    EffectTypeRegistry,
    # External API
    ExternalAPICall,
    FileCreate,
    FileDelete,
    FilePatch,
    # File domain
    FileRead,
    InputProvided,
    LifecyclePhaseCompleted,
    # Lifecycle phase
    LifecyclePhaseStarted,
    OutputProduced,
    # Agent trace
    PromptSent,
    StageCompleted,
    StageFailed,
    # Pipeline stage lifecycle
    StageSkipped,
    StageStarted,
    StepCompleted,
    StepFailed,
    # Step lifecycle
    StepStarted,
    TaskCompleted,
    TaskFailed,
    # Task lifecycle
    TaskStarted,
    ToolCallCompleted,
    ToolCallRejected,
    # Tool effects
    ToolCallStarted,
    WorkspaceMaterialized,
    effect_from_dict,
    get_effect_class,
    get_effect_type,
    register_effect,
)

# =============================================================================
# Errors
# =============================================================================
from shepherd_core.errors import (
    ArtifactNotFoundError,
    BindingNotFoundError,
    BindingValidationError,
    CapabilityError,
    # Capture phase
    CaptureError,
    CheckFailedError,
    ConfigurationError,
    # Task/Step
    ContextResolutionError,
    # Execute phase
    ExecutionError,
    OutputValidationError,
    # Prepare phase
    PreparationError,
    ProviderNotFoundError,
    RollbackError,
    # Configure phase
    ScopeNotConfiguredError,
    SDKExecutionError,
    # Session
    SessionCWDMismatchError,
    # Base
    ShepherdError,
    TaskExecutionError,
)
from shepherd_core.foundation import (
    ContainmentError,
    EffectLayerProtocol,
    # Protocols
    EffectProtocol,
    # Errors
    ScopeError,
    ScopeProtocol,
    StreamProtocol,
    # Layer 0: The fold
    fold,
    fold_until,
    fold_with_index,
    scan,
)

# Layer 3: Provider
from shepherd_core.provider import Provider
from shepherd_core.provider.runtime import EffectSink, ProviderRuntime

# =============================================================================
# Core Components
# =============================================================================
# Layer 1: Scope substrate
from shepherd_core.scope.context_ref import ContextRef
from shepherd_core.scope.model import ContextBinding, ImmutableScope
from shepherd_core.scope.stream import EffectLayer, Stream

# =============================================================================
# Core Types
# =============================================================================
from shepherd_core.types import (
    # Capability mapping
    CAPABILITY_TOOL_MAP,
    TOOL_CAPABILITY_REQUIREMENTS,
    # Results
    ExecutionResult,
    ProviderBinding,
    # Provider
    ProviderCapabilities,
    # Reversibility
    ReversibilityLevel,
    # Tool types
    ToolCall,
    ToolContext,
    ToolDefinition,
    ToolHandler,
    ToolHandlerWithContext,
    ToolResult,
    ToolValidator,
    # Trace config
    TraceConfig,
    ValidationResult,
    capability_for_tool,
    tools_for_capabilities,
)

for _shadowed_runtime_module in ("step", "combinators", "lifecycle"):
    _loaded_module = globals().get(_shadowed_runtime_module)
    if getattr(_loaded_module, "__name__", None) == f"{__name__}.{_shadowed_runtime_module}":
        globals().pop(_shadowed_runtime_module, None)

# =============================================================================
# Package Abstraction
# =============================================================================
from shepherd_core.package import (
    PackageInfo,
    discover_packages,
    get_package_registry,
    package,
)

# =============================================================================
# Run Convenience Functions
# =============================================================================
from shepherd_core.run import (
    run,
    run_sync,
)

# =============================================================================
# Exports
# =============================================================================

__all__ = [  # noqa: RUF022
    "CAPABILITY_TOOL_MAP",
    "EFFECT_TYPES",
    "EffectTypeRegistry",
    "TOOL_CAPABILITY_REQUIREMENTS",
    "AgentMessage",
    "AgentThinking",
    # Errors
    "ShepherdError",
    "ArtifactMissing",
    "ArtifactNotFoundError",
    "ArtifactWritten",
    "BindingNotFoundError",
    "BindingValidationError",
    "CapabilityError",
    "CaptureError",
    "CheckFailedError",
    "ConfigurationError",
    "ContainmentError",
    "ContextBinding",
    "ContextCaptured",
    "ContextCleanedUp",
    "ContextConfigured",
    "ContextPrepared",
    "ContextRef",
    "ContextResolutionError",
    # Data types
    "DiffPatch",
    # Effects
    "Effect",
    "EffectLayer",
    "EffectLayerProtocol",
    # Protocols
    "EffectProtocol",
    "EffectSink",
    # =========================================================================
    # Advanced API (Power Users)
    # =========================================================================
    # Context protocol
    "ExecutionContext",
    "ExecutionContextDefaults",
    "ExecutionError",
    "ExecutionResult",
    # Scope substrate
    "ImmutableScope",
    "ExternalAPICall",
    "FileCreate",
    "FileDelete",
    "FilePatch",
    "FileRead",
    # Autoconfig
    "Infer",
    "_InferMarker",
    "build_inference_model",
    "extract_infer_fields",
    "InputProvided",
    "KERNEL_EFFECT_REGISTRY",
    "LifecyclePhaseCompleted",
    "LifecyclePhaseStarted",
    "OutputProduced",
    "OutputValidationError",
    # Package abstraction
    "PackageInfo",
    "PreparationError",
    "PromptSent",
    # Provider (Layer 3)
    "Provider",
    "ProviderBinding",
    "ProviderCapabilities",
    "ProviderNotFoundError",
    "ProviderRuntime",
    # Types
    "ReversibilityLevel",
    "RollbackError",
    "SDKExecutionError",
    # Errors
    "ScopeError",
    "ScopeNotConfiguredError",
    "ScopeProtocol",
    "SessionCWDMismatchError",
    "StageCompleted",
    "StageFailed",
    # Pipeline stage lifecycle
    "StageSkipped",
    "StageStarted",
    "StepCompleted",
    "StepFailed",
    "StepStarted",
    # Stream
    "Stream",
    "StreamProtocol",
    "TaskCompleted",
    "TaskExecutionError",
    "TaskFailed",
    "TaskStarted",
    "ToolCall",
    "ToolCallCompleted",
    "ToolCallRejected",
    "ToolCallStarted",
    "ToolContext",
    "ToolDefinition",
    "ToolHandler",
    "ToolHandlerWithContext",
    "ToolResult",
    "ToolValidator",
    "TraceConfig",
    "ValidationResult",
    "WorkspaceMaterialized",
    # Version
    "__version__",
    "capability_for_tool",
    "compute_composite_reversibility",
    "discover_packages",
    "effect_from_dict",
    # The fold (core invariant)
    "fold",
    "fold_until",
    "fold_with_index",
    "get_effect_class",
    "get_effect_type",
    "get_package_registry",
    "is_execution_context",
    "is_reversible",
    "is_strict_mode",
    # Package abstraction
    "package",
    "register_effect",
    # Run convenience functions
    "run",
    "run_sync",
    "scan",
    "set_strict_mode",
    "tools_for_capabilities",
    "type_to_json_schema",
]
