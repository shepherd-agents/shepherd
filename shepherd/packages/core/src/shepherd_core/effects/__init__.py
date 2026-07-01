"""Effect system for shepherd-core.

This package defines all effect types used in the framework:
- Effect: Base class with attribution metadata
- Task lifecycle: TaskStarted, TaskCompleted, TaskFailed
- Context lifecycle: ContextConfigured, ContextPrepared, ContextCaptured, ContextCleanedUp
- Tool effects: ToolCallStarted, ToolCallCompleted, ToolCallRejected
- Domain effects: File*, Session*, Transfer*, ExternalAPICall

Also provides:
- Views: Filtered perspectives on effect streams (intents, outcomes, costs, thinking, causality)
- Formatters: Convert streams to various formats (markdown, compact, JSON, tree)
- Comparison: Compare streams, detect patterns, find anomalies

All effects are immutable (frozen Pydantic models) and carry attribution
metadata for filtering and debugging.
"""

from __future__ import annotations

from .commons_vcs import (
    SHEPHERD_CAUSED_BY_ROLE,
    SHEPHERD_EFFECT_PROJECTION_VERSION,
    SHEPHERD_EFFECT_ROLE,
    SHEPHERD_EFFECT_SCHEMA,
    SHEPHERD_EVENT_SCHEMA,
    SHEPHERD_PREVIOUS_ROLE,
    ProjectedEffectLayer,
    ProjectedEffectStream,
    normalize_commons_value,
    project_effect_layer,
    project_effect_object,
    project_effect_stream,
    project_event_layer,
    shepherd_effect_profile,
    validate_shepherd_effect_v1,
    validate_shepherd_event_v1,
)
from .comparison import (
    CRITICAL_THRESHOLD,
    IMPORTANT_THRESHOLD,
    ComparisonConfig,
    ComparisonResult,
    Divergence,
    EffectPattern,
    FileAccessComparison,
    ReferenceCorpus,
    compare_file_access,
    compare_streams,
    compare_tool_sequences,
    detect_patterns,
    explain_outcome_difference,
    find_anomalies,
)
from .contributors import (
    EFFECTS_GROUP,
    EffectContributorConflictError,
    EffectContributorNameConflictError,
    EffectContributorValidationError,
    discover_effects,
)
from .effects import (
    # Registry and utilities
    EFFECT_TYPES,
    # Intent effect classification
    INTENT_EFFECT_TYPES,
    KERNEL_EFFECT_REGISTRY,
    # Large content handling
    MAX_CONTENT_SIZE,
    # Preview length constants
    PREVIEW_LENGTH_API_BODY,
    PREVIEW_LENGTH_ARTIFACT,
    PREVIEW_LENGTH_FILE_CONTENT,
    PREVIEW_LENGTH_PROMPT,
    PREVIEW_LENGTH_STEP_SUMMARY,
    PREVIEW_LENGTH_TOOL_OUTPUT,
    TRUNCATE_HEAD_SIZE,
    TRUNCATE_TAIL_SIZE,
    AgentMessage,
    AgentThinking,
    ArtifactMissing,
    # Artifact
    ArtifactWritten,
    # Container execution
    ContainerExecutionCompleted,
    ContextCaptured,
    ContextCleanedUp,
    # Context lifecycle
    ContextConfigured,
    # Context materialization
    ContextMaterialized,
    ContextPrepared,
    # Data types
    DiffPatch,
    # Base
    Effect,
    # Execution failure and recovery
    ExecutionFailed,
    # External API
    ExternalAPICall,
    FileCreate,
    FileDelete,
    FilePatch,
    # File domain
    FileRead,
    InputProvided,
    # Type aliases
    LifecyclePhase,
    LifecyclePhaseCompleted,
    LifecyclePhaseFailed,
    # Lifecycle phase effects
    LifecyclePhaseStarted,
    # LLM response profiling
    LLMResponseReceived,
    OutputProduced,
    # Agent trace
    PromptSent,
    RecoveryAttempted,
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
    ToolCallBatch,
    ToolCallCompleted,
    ToolCallInfo,
    ToolCallRejected,
    # Tool effects
    ToolCallStarted,
    # Workspace materialization
    WorkspaceMaterialized,
    effect_from_dict,
    get_effect_class,
    get_effect_type,
    is_intent_effect,
    is_result_effect,
    register_effect,
    truncate_with_hash,
)
from .formatters import (
    CompactFormatter,
    EffectFormatter,
    FormatterOptions,
    JSONFormatter,
    MarkdownFormatter,
    TreeFormatter,
    format_profile,
)
from .registry import EffectTypeRegistry
from .views import (
    CausalityNode,
    CausalityTreeView,
    CostSummary,
    CostsView,
    IntentsView,
    ModelProfile,
    OutcomesView,
    ProfileSummary,
    ProfileView,
    RecoverySummary,
    StreamView,
    TaskCacheSummary,
    TaskNode,
    TaskProfile,
    ThinkingView,
    TimeBreakdown,
    ToolProfile,
)

__all__ = [  # noqa: RUF022
    # Comparison
    "CRITICAL_THRESHOLD",
    # Registry and utilities
    "EFFECT_TYPES",
    "EFFECTS_GROUP",
    "EffectTypeRegistry",
    "EffectContributorConflictError",
    "EffectContributorNameConflictError",
    "EffectContributorValidationError",
    "IMPORTANT_THRESHOLD",
    "KERNEL_EFFECT_REGISTRY",
    # Intent effect classification
    "INTENT_EFFECT_TYPES",
    # Large content handling
    "MAX_CONTENT_SIZE",
    "PREVIEW_LENGTH_API_BODY",
    "PREVIEW_LENGTH_ARTIFACT",
    "PREVIEW_LENGTH_FILE_CONTENT",
    "PREVIEW_LENGTH_PROMPT",
    "PREVIEW_LENGTH_STEP_SUMMARY",
    # Preview length constants
    "PREVIEW_LENGTH_TOOL_OUTPUT",
    "TRUNCATE_HEAD_SIZE",
    "TRUNCATE_TAIL_SIZE",
    "AgentMessage",
    "AgentThinking",
    "ArtifactMissing",
    "SHEPHERD_CAUSED_BY_ROLE",
    "SHEPHERD_EFFECT_PROJECTION_VERSION",
    "SHEPHERD_EFFECT_ROLE",
    "SHEPHERD_EFFECT_SCHEMA",
    "SHEPHERD_EVENT_SCHEMA",
    "SHEPHERD_PREVIOUS_ROLE",
    # Artifact
    "ArtifactWritten",
    "CausalityNode",
    "CausalityTreeView",
    "CompactFormatter",
    "ComparisonConfig",
    "ComparisonResult",
    # Container execution
    "ContainerExecutionCompleted",
    "ContextCaptured",
    "ContextCleanedUp",
    # Context lifecycle
    "ContextConfigured",
    # Context materialization
    "ContextMaterialized",
    "ContextPrepared",
    "CostSummary",
    "CostsView",
    # Data types
    "DiffPatch",
    "Divergence",
    # Base
    "Effect",
    "EffectFormatter",
    "EffectPattern",
    # Execution failure and recovery
    "ExecutionFailed",
    # External API
    "ExternalAPICall",
    "FileAccessComparison",
    "FileCreate",
    "FileDelete",
    "FilePatch",
    # File domain
    "FileRead",
    # Formatters
    "FormatterOptions",
    "InputProvided",
    "IntentsView",
    "JSONFormatter",
    # LLM response profiling
    "LLMResponseReceived",
    # Type aliases
    "LifecyclePhase",
    "LifecyclePhaseCompleted",
    "LifecyclePhaseFailed",
    # Lifecycle phase effects
    "LifecyclePhaseStarted",
    "MarkdownFormatter",
    "ModelProfile",
    "OutcomesView",
    "OutputProduced",
    # Agent trace
    "ProfileSummary",
    "ProfileView",
    "ProjectedEffectLayer",
    "ProjectedEffectStream",
    "PromptSent",
    "RecoveryAttempted",
    "RecoverySummary",
    "ReferenceCorpus",
    "StageCompleted",
    "StageFailed",
    # Pipeline stage lifecycle
    "StageSkipped",
    "StageStarted",
    "StepCompleted",
    "StepFailed",
    # Step lifecycle
    "StepStarted",
    # Views
    "StreamView",
    "TaskCacheSummary",
    "TaskCompleted",
    "TaskFailed",
    "TaskNode",
    "TaskProfile",
    # Task lifecycle
    "TaskStarted",
    "ThinkingView",
    "TimeBreakdown",
    "ToolCallBatch",
    "ToolCallCompleted",
    "ToolCallInfo",
    "ToolCallRejected",
    # Tool effects
    "ToolCallStarted",
    "ToolProfile",
    "TreeFormatter",
    # Workspace materialization
    "WorkspaceMaterialized",
    "compare_file_access",
    "compare_streams",
    "compare_tool_sequences",
    "detect_patterns",
    "discover_effects",
    "effect_from_dict",
    "explain_outcome_difference",
    "find_anomalies",
    "format_profile",
    "get_effect_class",
    "get_effect_type",
    "shepherd_effect_profile",
    "is_intent_effect",
    "is_result_effect",
    "normalize_commons_value",
    "project_effect_layer",
    "project_effect_object",
    "project_effect_stream",
    "project_event_layer",
    "register_effect",
    "truncate_with_hash",
    "validate_shepherd_effect_v1",
    "validate_shepherd_event_v1",
]
