"""Cross-package import smoke tests for owner paths and removed hard-cut surfaces."""

import importlib
import inspect

import pytest


class TestCoreImports:
    """Test kernel imports plus removed hard-cut surfaces."""

    def test_core_primitives(self) -> None:
        """Kernel primitives remain importable from the core root."""
        from shepherd_core import (
            Effect,
            ExecutionContext,
            Provider,
            ProviderBinding,
            Stream,
        )

        assert inspect.isclass(Provider), "Provider should be a class"
        assert inspect.isclass(ExecutionContext), "ExecutionContext should be a class"
        assert inspect.isclass(Effect), "Effect should be a class"
        assert inspect.isclass(Stream), "Stream should be a class"
        assert inspect.isclass(ProviderBinding), "ProviderBinding should be a class"

    def test_core_root_no_longer_exports_scope_shell(self) -> None:
        """Runtime-owned Scope APIs should no longer resolve from the core root."""
        module = importlib.reload(importlib.import_module("shepherd_core"))

        assert not hasattr(module, "Scope")
        assert not hasattr(module, "current_scope")
        assert not hasattr(module, "require_scope")

    def test_core_root_no_longer_exports_runtime_context_compat_symbols(self) -> None:
        """Runtime-owned context helpers should no longer resolve from the core root."""
        module = importlib.reload(importlib.import_module("shepherd_core"))

        assert not hasattr(module, "Bindable")
        assert not hasattr(module, "BindableContext")

    def test_core_root_all_tracks_kernel_contract_only(self) -> None:
        """The core root barrel should advertise only kernel exports."""
        module = importlib.reload(importlib.import_module("shepherd_core"))

        assert "Effect" in module.__all__
        assert "type_to_json_schema" in module.__all__
        assert "Scope" not in module.__all__
        assert "task" not in module.__all__
        assert "Input" not in module.__all__
        assert "ExecutionLifecycle" not in module.__all__
        assert "gate" not in module.__all__
        assert "export_json" not in module.__all__

    def test_core_root_dir_prefers_kernel_contract_until_compat_loaded(self) -> None:
        """dir(shepherd_core) should not advertise runtime exports by default."""
        module = importlib.reload(importlib.import_module("shepherd_core"))

        assert "Scope" not in dir(module)
        assert "Effect" in dir(module)
        assert "task" not in dir(module)
        assert "ExecutionLifecycle" not in dir(module)
        assert "step" not in dir(module)
        assert "gate" not in dir(module)

    def test_core_no_longer_exports_database_effect_family(self) -> None:
        """Database-owned effects should no longer resolve from core barrels."""
        core_module = importlib.reload(importlib.import_module("shepherd_core"))
        effects_module = importlib.reload(importlib.import_module("shepherd_core.effects"))

        assert not hasattr(core_module, "QueryExecuted")
        assert not hasattr(effects_module, "QueryExecuted")

    def test_core_no_longer_exports_context_effect_families(self) -> None:
        """Context-owned effect families should no longer resolve from core barrels."""
        core_module = importlib.reload(importlib.import_module("shepherd_core"))
        effects_module = importlib.reload(importlib.import_module("shepherd_core.effects"))

        for symbol_name in (
            "BashCommand",
            "MCPToolCalled",
            "SessionCreated",
            "SessionForked",
            "SessionResumed",
            "WorkspacePatchCaptured",
        ):
            assert not hasattr(core_module, symbol_name), f"{symbol_name} should not be exported from shepherd_core"
            assert not hasattr(effects_module, symbol_name), (
                f"{symbol_name} should not be exported from shepherd_core.effects"
            )

    def test_core_effect_package_owns_effect_contributor_discovery(self) -> None:
        """Effect contributor discovery should live under the core effects owner path."""
        effects_module = importlib.reload(importlib.import_module("shepherd_core.effects"))

        assert hasattr(effects_module, "discover_effects")

        # _shared.registry has been deleted; verify it is no longer importable.
        with pytest.raises(ModuleNotFoundError):
            importlib.import_module("shepherd_core._shared.registry")

    def test_core_root_no_longer_exports_export_apis(self) -> None:
        """Export/import APIs should no longer resolve from the core root."""
        module = importlib.reload(importlib.import_module("shepherd_core"))

        for symbol_name in (
            "ScopeInfo",
            "TrajectoryResult",
            "export_json",
            "from_trajectory",
            "import_json",
            "to_trajectory",
        ):
            assert not hasattr(module, symbol_name), f"{symbol_name} should not be exported from shepherd_core"

    def test_core_root_no_longer_exports_transform_apis(self) -> None:
        """Transform-owned APIs should no longer resolve from the core root."""
        module = importlib.reload(importlib.import_module("shepherd_core"))

        for symbol_name in (
            "CoverageReport",
            "CritiqueTask",
            "EquivalenceLevel",
            "EquivalenceResult",
            "GroundingResult",
            "Mismatch",
            "OptimizeFromEffects",
            "TaskInputSpec",
            "TestInputGenerator",
            "TransformTask",
            "analyze_coverage",
            "behavioral_grounding",
            "compare_at_level",
            "compare_outcome",
            "compare_relaxed",
            "compare_semantic",
            "compare_strict",
            "ground_transformation",
        ):
            assert not hasattr(module, symbol_name), f"{symbol_name} should not be exported from shepherd_core"

    def test_core_root_no_longer_exports_handler_apis(self) -> None:
        """Runtime-owned handler APIs should no longer resolve from the core root."""
        module = importlib.reload(importlib.import_module("shepherd_core"))

        for symbol_name in (
            "CompositeHandler",
            "EffectHandler",
            "HandlerContext",
            "HandlerNotFoundError",
            "HandlerRegistry",
            "LoggingHandler",
            "MaterializationError",
            "Materializer",
            "PassthroughHandler",
            "ReversalError",
            "SimpleHandlerContext",
            "get_default_registry",
            "get_handler",
            "register_handler",
            "reset_default_registry",
        ):
            assert not hasattr(module, symbol_name), f"{symbol_name} should not be exported from shepherd_core"

    def test_core_scope_package_no_longer_exports_runtime_materialization_apis(self) -> None:
        """Runtime-owned materialization APIs should not resolve from the scope barrel."""
        module = importlib.reload(importlib.import_module("shepherd_core.scope"))

        for symbol_name in (
            "ContextMaterializer",
            "Materializable",
            "MaterializationIntent",
            "MaterializationResult",
            "Materializer",
            "clear_context_materializer_registry",
            "clear_materializer_registry",
            "get_context_materializer",
            "get_materializer",
            "is_materializable",
            "register_context_materializer",
            "register_materializer",
        ):
            assert not hasattr(module, symbol_name), f"{symbol_name} should not be exported from shepherd_core.scope"

    def test_core_root_no_longer_exports_runtime_task_step_lifecycle_or_combinator_apis(self) -> None:
        """Runtime-owned task/step/lifecycle/combinator APIs should not resolve from the core root."""
        module = importlib.reload(importlib.import_module("shepherd_core"))

        for symbol_name in (
            "Artifact",
            "ArtifactMarker",
            "BoundStepBuilder",
            "Budget",
            "Check",
            "CompletedTask",
            "Context",
            "ContextMarker",
            "DEFAULT_STEP_TIMEOUT",
            "DisjointMerge",
            "EffectConflictError",
            "EffectPredicate",
            "ExecutionLifecycle",
            "FieldInfo",
            "FileExists",
            "InRange",
            "InlineStep",
            "Input",
            "InputMarker",
            "JudgePredicate",
            "LastWriteWins",
            "Matches",
            "MaxLength",
            "MergeStrategy",
            "NonEmpty",
            "OnError",
            "OnErrorPolicy",
            "Output",
            "OutputMarker",
            "Predicate",
            "Rejected",
            "SINGLE_OUTPUT_KEY",
            "StepBuilder",
            "StepExecutionError",
            "StepInputInfo",
            "StepMetadata",
            "StepOutputError",
            "TaskMetadata",
            "TaskRef",
            "TaskRefReconstructionPolicy",
            "branch",
            "budget",
            "collect_artifacts",
            "eval_predicate",
            "execute",
            "extract_task_imports",
            "extract_task_metadata",
            "extract_task_source",
            "extract_task_with_imports",
            "fallback",
            "filter_effects",
            "gate",
            "loop",
            "map_effects",
            "parallel",
            "parallel_all",
            "race",
            "read_artifact",
            "reconstruct_task_class",
            "recover",
            "retry",
            "scope_tap",
            "secure_reconstruct_task_class",
            "sequence",
            "sequence_all",
            "should_parse_json",
            "step",
            "tap",
            "task",
            "timeout",
            "validate_task_source",
        ):
            assert not hasattr(module, symbol_name), f"{symbol_name} should not be exported from shepherd_core"

    def test_core_device_namespace_removed(self) -> None:
        """The core device namespace has been fully removed after extraction."""
        with pytest.raises(ModuleNotFoundError):
            importlib.import_module("shepherd_core.device")

    def test_core_testing(self) -> None:
        """Testing utilities are importable."""
        from shepherd_tests import MockProvider

        # Verify MockProvider is a class
        assert inspect.isclass(MockProvider), "MockProvider should be a class"


class TestProviderImports:
    """Test shepherd-providers imports."""

    def test_claude_provider(self) -> None:
        """ClaudeProvider is importable."""
        from shepherd_providers.claude import ClaudeProvider

        assert inspect.isclass(ClaudeProvider), "ClaudeProvider should be a class"

    def test_openai_provider(self) -> None:
        """OpenAIProvider is importable."""
        from shepherd_providers.openai import OpenAIProvider

        assert inspect.isclass(OpenAIProvider), "OpenAIProvider should be a class"

    def test_root_imports(self) -> None:
        """Root package exports providers."""
        from shepherd_providers import ClaudeProvider, OpenAIProvider

        assert inspect.isclass(ClaudeProvider), "ClaudeProvider should be a class"
        assert inspect.isclass(OpenAIProvider), "OpenAIProvider should be a class"


class TestSandboxImports:
    """Test shepherd-sandboxes imports."""

    def test_root_imports(self) -> None:
        """Root package exports remote sandbox wrappers."""
        from shepherd_sandboxes import DaytonaSandbox, E2BSandbox, K8sSandbox, ModalSandbox, PrimeSandbox

        assert inspect.isclass(DaytonaSandbox), "DaytonaSandbox should be a class"
        assert inspect.isclass(E2BSandbox), "E2BSandbox should be a class"
        assert inspect.isclass(K8sSandbox), "K8sSandbox should be a class"
        assert inspect.isclass(ModalSandbox), "ModalSandbox should be a class"
        assert inspect.isclass(PrimeSandbox), "PrimeSandbox should be a class"


class TestTransformImports:
    """Test shepherd-transform imports."""

    def test_root_imports(self) -> None:
        """Root package exports transform and grounding APIs."""
        from shepherd_transform import (
            CritiqueTask,
            EquivalenceLevel,
            OptimizeFromEffects,
            TaskInputSpec,
            TestInputGenerator,
            TransformTask,
            behavioral_grounding,
        )

        assert inspect.isclass(CritiqueTask), "CritiqueTask should be a class"
        assert inspect.isclass(OptimizeFromEffects), "OptimizeFromEffects should be a class"
        assert inspect.isclass(TransformTask), "TransformTask should be a class"
        assert inspect.isclass(EquivalenceLevel), "EquivalenceLevel should be a class"
        assert inspect.isclass(TaskInputSpec), "TaskInputSpec should be a class"
        assert inspect.isclass(TestInputGenerator), "TestInputGenerator should be a class"
        assert callable(behavioral_grounding), "behavioral_grounding should be callable"

    def test_submodule_imports(self) -> None:
        """Explicit transform owner paths are importable."""
        from shepherd_transform.grounding import EquivalenceLevel, GroundingResult
        from shepherd_transform.meta import CritiqueTask, OptimizeFromEffects, TransformTask

        assert inspect.isclass(CritiqueTask), "CritiqueTask should be a class"
        assert inspect.isclass(OptimizeFromEffects), "OptimizeFromEffects should be a class"
        assert inspect.isclass(TransformTask), "TransformTask should be a class"
        assert inspect.isclass(EquivalenceLevel), "EquivalenceLevel should be a class"
        assert inspect.isclass(GroundingResult), "GroundingResult should be a class"

    def test_sandbox_submodule_imports(self) -> None:
        """Explicit sandbox owner paths are importable."""
        from shepherd_sandboxes.daytona import DaytonaSandbox
        from shepherd_sandboxes.e2b import E2BSandbox
        from shepherd_sandboxes.kubernetes import K8sSandbox
        from shepherd_sandboxes.modal import ModalSandbox
        from shepherd_sandboxes.prime import PrimeSandbox

        assert inspect.isclass(DaytonaSandbox), "DaytonaSandbox should be a class"
        assert inspect.isclass(E2BSandbox), "E2BSandbox should be a class"
        assert inspect.isclass(K8sSandbox), "K8sSandbox should be a class"
        assert inspect.isclass(ModalSandbox), "ModalSandbox should be a class"
        assert inspect.isclass(PrimeSandbox), "PrimeSandbox should be a class"


class TestRuntimeOwnerPaths:
    """Test explicit runtime owner paths added during boundary hardening."""

    def test_lifecycle_owner_paths(self) -> None:
        """Runtime lifecycle shims are importable."""
        from shepherd_runtime.lifecycle import ExecutionLifecycle, execute, register_sandbox_factory

        assert inspect.isclass(ExecutionLifecycle), "ExecutionLifecycle should be a class"
        assert callable(execute), "execute should be callable"
        assert callable(register_sandbox_factory), "register_sandbox_factory should be callable"

    def test_scope_owner_paths(self) -> None:
        """Runtime scope shims are importable."""
        from shepherd_runtime.scope import Scope, ScopeProxy, current_scope, require_scope

        assert inspect.isclass(Scope), "Scope should be a class"
        assert inspect.isclass(ScopeProxy), "ScopeProxy should be a class"
        assert callable(current_scope), "current_scope should be callable"
        assert callable(require_scope), "require_scope should be callable"

    def test_materialization_owner_paths(self) -> None:
        """Runtime context-materialization shims are importable."""
        from shepherd_runtime.materialization import (
            MaterializationIntent,
            MaterializationResult,
            register_context_materializer,
            register_materializer,
        )

        assert inspect.isclass(MaterializationIntent), "MaterializationIntent should be a class"
        assert inspect.isclass(MaterializationResult), "MaterializationResult should be a class"
        assert callable(register_context_materializer), "register_context_materializer should be callable"
        assert callable(register_materializer), "register_materializer should be callable"

    def test_handler_owner_paths(self) -> None:
        """Runtime handler shims are importable."""
        from shepherd_runtime.handlers import (
            CompositeHandler,
            HandlerContext,
            HandlerRegistry,
            LoggingHandler,
            PassthroughHandler,
            SimpleHandlerContext,
            get_default_registry,
            register_handler,
            reset_default_registry,
        )

        assert inspect.isclass(CompositeHandler), "CompositeHandler should be a class"
        assert inspect.isclass(HandlerRegistry), "HandlerRegistry should be a class"
        assert inspect.isclass(LoggingHandler), "LoggingHandler should be a class"
        assert inspect.isclass(PassthroughHandler), "PassthroughHandler should be a class"
        assert inspect.isclass(SimpleHandlerContext), "SimpleHandlerContext should be a class"
        assert hasattr(HandlerContext, "__dict__"), "HandlerContext should be importable"
        assert callable(get_default_registry), "get_default_registry should be callable"
        assert callable(register_handler), "register_handler should be callable"
        assert callable(reset_default_registry), "reset_default_registry should be callable"

    def test_core_handler_module_removed(self) -> None:
        """Old core handler package should no longer be importable."""
        with pytest.raises(ModuleNotFoundError):
            importlib.import_module("shepherd_core.handlers")

    def test_core_step_module_removed(self) -> None:
        """Old core step package should no longer be importable."""
        with pytest.raises(ModuleNotFoundError):
            importlib.import_module("shepherd_core.step")

    def test_core_combinator_module_removed(self) -> None:
        """Old core combinator package should no longer be importable."""
        with pytest.raises(ModuleNotFoundError):
            importlib.import_module("shepherd_core.combinators")

    def test_core_lifecycle_module_removed(self) -> None:
        """Old core lifecycle package should no longer be importable."""
        with pytest.raises(ModuleNotFoundError):
            importlib.import_module("shepherd_core.lifecycle")

    def test_core_checkpoint_module_removed(self) -> None:
        """Old core checkpoint module should no longer be importable."""
        with pytest.raises(ModuleNotFoundError):
            importlib.import_module("shepherd_core.scope.checkpoint")

    def test_core_cache_module_removed(self) -> None:
        """Old core cache package should no longer be importable."""
        with pytest.raises(ModuleNotFoundError):
            importlib.import_module("shepherd_core.cache")

    def test_core_persistence_module_removed(self) -> None:
        """Old core persistence package should no longer be importable."""
        with pytest.raises(ModuleNotFoundError):
            importlib.import_module("shepherd_core.persistence")

    def test_core_task_module_removed(self) -> None:
        """Old core task package should no longer be importable."""
        with pytest.raises(ModuleNotFoundError):
            importlib.import_module("shepherd_core.task")

    def test_core_export_module_removed(self) -> None:
        """Old core export package should no longer be importable."""
        with pytest.raises(ModuleNotFoundError):
            importlib.import_module("shepherd_core.export")

    def test_core_transform_modules_removed(self) -> None:
        """Old core transform packages should no longer be importable."""
        for module_name in (
            "shepherd_core.grounding",
            "shepherd_core.meta",
        ):
            with pytest.raises(ModuleNotFoundError):
                importlib.import_module(module_name)

    def test_runtime_transform_task_modules_removed(self) -> None:
        """Old runtime task transform modules should no longer be importable."""
        for module_name in (
            "shepherd_runtime.task.chaining",
            "shepherd_runtime.task.transform_lock",
        ):
            with pytest.raises(ModuleNotFoundError):
                importlib.import_module(module_name)

    def test_core_device_runtime_modules_removed(self) -> None:
        """Old core runtime-owned device modules should no longer be importable."""
        for module_name in (
            "shepherd_core.device.local",
            "shepherd_core.device.transfer",
            "shepherd_core.device.errors",
            "shepherd_core.device.container",
            "shepherd_core.device.daytona",
            "shepherd_core.device.e2b",
            "shepherd_core.device.kubernetes",
            "shepherd_core.device.modal",
            "shepherd_core.device.prime",
        ):
            with pytest.raises(ModuleNotFoundError):
                importlib.import_module(module_name)

    def test_context_owner_paths(self) -> None:
        """Runtime context and sandbox shims are importable."""
        from shepherd_runtime.context import Bindable, BindableContext, NullSandbox, Sandbox
        from shepherd_runtime.context.sandbox import GitWorktreeSandbox

        assert inspect.isclass(Bindable), "Bindable should be a class"
        assert inspect.isclass(BindableContext), "BindableContext should be a class"
        assert inspect.isclass(NullSandbox), "NullSandbox should be a class"
        assert inspect.isclass(GitWorktreeSandbox), "GitWorktreeSandbox should be a class"
        assert hasattr(Sandbox, "__dict__"), "Sandbox should be importable"

    def test_core_context_modules_no_longer_export_runtime_symbols(self) -> None:
        """Core context barrels should remain kernel-only after hard cuts."""
        core_context = importlib.reload(importlib.import_module("shepherd_core.context"))
        core_protocol = importlib.reload(importlib.import_module("shepherd_core.context.protocol"))
        assert not hasattr(core_context, "Bindable")
        assert not hasattr(core_context, "BindableContext")
        assert not hasattr(core_context, "NullSandbox")
        assert not hasattr(core_context, "Sandbox")
        assert not hasattr(core_context, "GitWorktreeSandbox")
        assert not hasattr(core_protocol, "Bindable")
        assert not hasattr(core_protocol, "BindableContext")
        assert not hasattr(core_protocol, "NullSandbox")
        assert not hasattr(core_protocol, "RuntimeContextDefaults")
        assert not hasattr(core_protocol, "Sandbox")
        assert core_context.ExecutionContext is not None
        assert core_context.ExecutionContextDefaults is not None
        assert core_protocol.ExecutionContext is not None
        assert core_protocol.ExecutionContextDefaults is not None

    def test_removed_core_context_and_materialization_compat_modules(self) -> None:
        """Hard-cut core compat modules should no longer be importable."""
        for module_name in (
            "shepherd_core.context.runtime",
            "shepherd_core.context.sandbox",
            "shepherd_core.scope.materialization",
        ):
            with pytest.raises(ModuleNotFoundError):
                importlib.import_module(module_name)

    def test_device_transfer_owner_paths(self) -> None:
        """Runtime device-transfer shims are importable."""
        from shepherd_runtime.device.transfer import (
            TransferBundle,
            collect_visible_patches,
            compute_content_hash,
        )

        assert inspect.isclass(TransferBundle), "TransferBundle should be a class"
        assert callable(collect_visible_patches), "collect_visible_patches should be callable"
        assert callable(compute_content_hash), "compute_content_hash should be callable"

    def test_cache_owner_paths(self) -> None:
        """Runtime cache shims are importable."""
        from shepherd_runtime.cache import CacheHit, CachePolicy, ExecutionKey, HashingScope

        assert inspect.isclass(CacheHit), "CacheHit should be a class"
        assert inspect.isclass(CachePolicy), "CachePolicy should be a class"
        assert inspect.isclass(ExecutionKey), "ExecutionKey should be a class"
        assert inspect.isclass(HashingScope), "HashingScope should be a class"

    def test_persistence_owner_paths(self) -> None:
        """Runtime persistence shims are importable."""
        from shepherd_runtime.persistence import PersistenceConfig, PersistenceManager, ProjectId

        assert inspect.isclass(PersistenceConfig), "PersistenceConfig should be a class"
        assert inspect.isclass(PersistenceManager), "PersistenceManager should be a class"
        assert inspect.isclass(ProjectId), "ProjectId should be a class"

    def test_checkpoint_owner_paths(self) -> None:
        """Runtime checkpoint owner paths are importable."""
        from shepherd_runtime.checkpoint import Checkpoint, CheckpointValidationError

        assert inspect.isclass(Checkpoint), "Checkpoint should be a class"
        assert inspect.isclass(CheckpointValidationError), "CheckpointValidationError should be a class"

    def test_effect_registry_owner_paths(self) -> None:
        """Runtime effect registry composition is importable."""
        from shepherd_runtime.effects import compose_effect_registry

        assert callable(compose_effect_registry), "compose_effect_registry should be callable"

    def test_core_helper_owner_paths(self) -> None:
        """Public core helper owner paths are importable."""
        from shepherd_core.config import is_strict_mode, set_strict_mode
        from shepherd_core.text import smart_truncate

        assert callable(is_strict_mode), "is_strict_mode should be callable"
        assert callable(set_strict_mode), "set_strict_mode should be callable"
        assert callable(smart_truncate), "smart_truncate should be callable"
        assert is_strict_mode.__module__ == "shepherd_core.config"
        assert set_strict_mode.__module__ == "shepherd_core.config"
        assert smart_truncate.__module__ == "shepherd_core.text"

    def test_combinator_owner_paths(self) -> None:
        """Runtime combinator shims are importable."""
        from shepherd_runtime.combinators import Rejected, gate, retry

        assert inspect.isclass(Rejected), "Rejected should be a class"
        assert callable(gate), "gate should be callable"
        assert callable(retry), "retry should be callable"

    def test_device_owner_paths(self) -> None:
        """Runtime device shims are importable."""
        from shepherd_runtime.device import Device, LocalDevice, get_current_device, get_device, list_devices
        from shepherd_runtime.device.local import LocalSandboxHandle

        assert callable(Device), "Device should be callable"
        assert inspect.isclass(LocalDevice), "LocalDevice should be a class"
        assert inspect.isclass(LocalSandboxHandle), "LocalSandboxHandle should be a class"
        assert callable(get_current_device), "get_current_device should be callable"
        assert callable(get_device), "get_device should be callable"
        assert callable(list_devices), "list_devices should be callable"

    def test_container_leaf_owner_paths(self) -> None:
        """Runtime container leaf owner paths are importable."""
        from shepherd_runtime.device.container.context_registry import ContextDeserializationError
        from shepherd_runtime.device.container.effect_collector import EffectCollector
        from shepherd_runtime.device.container.fuse_overlay import FuseOverlayManager, fuse_overlayfs_available
        from shepherd_runtime.device.container.provider_registry import ProviderCreationError
        from shepherd_runtime.device.container.stack_hooks import StackHooks

        assert inspect.isclass(ContextDeserializationError), "ContextDeserializationError should be a class"
        assert inspect.isclass(EffectCollector), "EffectCollector should be a class"
        assert inspect.isclass(FuseOverlayManager), "FuseOverlayManager should be a class"
        assert inspect.isclass(ProviderCreationError), "ProviderCreationError should be a class"
        assert inspect.isclass(StackHooks), "StackHooks should be a class"
        assert callable(fuse_overlayfs_available), "fuse_overlayfs_available should be callable"

    def test_task_pipeline_owner_paths(self) -> None:
        """Runtime task-pipeline shims are importable."""
        from shepherd_runtime.task.pipeline import OnError, OnErrorPolicy

        assert inspect.isclass(OnError), "OnError should be a class"
        assert inspect.isclass(OnErrorPolicy), "OnErrorPolicy should be a class"

    def test_task_marker_owner_paths(self) -> None:
        """Runtime task marker owner paths are importable."""
        from shepherd_runtime.task.markers import Artifact, CompletedTask, Input, Output, TaskRef

        assert callable(Input), "Input should be callable"
        assert callable(Output), "Output should be callable"
        assert callable(Artifact), "Artifact should be callable"
        assert inspect.isclass(TaskRef), "TaskRef should be a class"
        assert inspect.isclass(CompletedTask), "CompletedTask should be a class"

    def test_task_artifact_owner_paths(self) -> None:
        """Runtime task artifact helpers are importable."""
        from shepherd_runtime.task.artifacts import collect_artifacts, read_artifact, should_parse_json

        assert callable(collect_artifacts), "collect_artifacts should be callable"
        assert callable(read_artifact), "read_artifact should be callable"
        assert callable(should_parse_json), "should_parse_json should be callable"

    def test_task_authoring_owner_paths(self) -> None:
        """Runtime task authoring shims are importable."""
        from shepherd_runtime.task.authoring import Context, Input, Output, task
        from shepherd_runtime.task.metadata import FieldInfo, TaskMetadata, extract_task_metadata

        assert callable(task), "task should be callable"
        assert callable(Input), "Input should be callable"
        assert callable(Output), "Output should be callable"
        assert callable(Context), "Context should be callable"
        assert inspect.isclass(FieldInfo), "FieldInfo should be a class"
        assert inspect.isclass(TaskMetadata), "TaskMetadata should be a class"
        assert callable(extract_task_metadata), "extract_task_metadata should be callable"

    def test_task_source_owner_paths(self) -> None:
        """Transform source facade and runtime secure engine are importable."""
        from shepherd_runtime.task.secure import secure_reconstruct_task_class
        from shepherd_transform.source import extract_task_source, reconstruct_task_class

        runtime_secure = importlib.import_module("shepherd_runtime.task.secure")
        with pytest.raises(ModuleNotFoundError):
            importlib.import_module("shepherd_runtime.task.source")
        assert callable(secure_reconstruct_task_class), "secure_reconstruct_task_class should be callable"
        assert callable(extract_task_source), "extract_task_source should be callable"
        assert callable(reconstruct_task_class), "reconstruct_task_class should be callable"
        assert not hasattr(runtime_secure, "ALLOWED_DUNDERS")
        assert not hasattr(runtime_secure, "ALLOWED_MODULES")
        assert not hasattr(runtime_secure, "FORBIDDEN_NAMES")
        assert not hasattr(runtime_secure, "ReconstructionResult")
        assert not hasattr(runtime_secure, "safe_reconstruct")

    def test_transform_owner_paths(self) -> None:
        """Transform-owned task transformation surfaces are importable."""
        from shepherd_transform.chaining import ChainResult, TransformationEngine
        from shepherd_transform.source import (
            ReconstructionError,
            ReconstructionResult,
            SourceExtractionError,
            SourceValidationError,
            extract_task_imports,
            extract_task_source,
            reconstruct_task_class,
            try_reconstruct_task_class,
            validate_task_source,
        )
        from shepherd_transform.transform_lock import TaskTransformLock

        with pytest.raises(ModuleNotFoundError):
            importlib.import_module("shepherd_transform.reconstruction")

        assert inspect.isclass(TransformationEngine), "TransformationEngine should be a class"
        assert inspect.isclass(ChainResult), "ChainResult should be a class"
        assert inspect.isclass(ReconstructionResult), "ReconstructionResult should be a class"
        assert inspect.isclass(ReconstructionError), "ReconstructionError should be a class"
        assert inspect.isclass(SourceExtractionError), "SourceExtractionError should be a class"
        assert inspect.isclass(SourceValidationError), "SourceValidationError should be a class"
        assert inspect.isclass(TaskTransformLock), "TaskTransformLock should be a class"
        assert callable(extract_task_imports), "extract_task_imports should be callable"
        assert callable(extract_task_source), "extract_task_source should be callable"
        assert callable(reconstruct_task_class), "reconstruct_task_class should be callable"
        assert callable(try_reconstruct_task_class), "try_reconstruct_task_class should be callable"
        assert callable(validate_task_source), "validate_task_source should be callable"

    def test_step_owner_paths(self) -> None:
        """Runtime step authoring shims are importable."""
        from shepherd_runtime.step.api import (
            DEFAULT_STEP_TIMEOUT,
            SINGLE_OUTPUT_KEY,
            StepExecutionError,
            StepMetadata,
            StepOutputError,
            step,
        )

        assert callable(step), "step should be callable"
        assert isinstance(DEFAULT_STEP_TIMEOUT, (int, float)), "DEFAULT_STEP_TIMEOUT should be numeric"
        assert isinstance(SINGLE_OUTPUT_KEY, str), "SINGLE_OUTPUT_KEY should be a string"
        assert inspect.isclass(StepMetadata), "StepMetadata should be a class"
        assert inspect.isclass(StepExecutionError), "StepExecutionError should be a class"
        assert inspect.isclass(StepOutputError), "StepOutputError should be a class"

    def test_effect_materialization_owner_paths(self) -> None:
        """Runtime effect-materialization shims are importable."""
        from shepherd_runtime.effect_materialization import (
            GitWorkspacePatchMaterializer,
            MaterializationResult,
            Materializer,
            MaterializerRegistry,
            create_workspace_materializer,
            get_materializer_registry_with_builtins,
            register_materializer,
        )

        assert inspect.isclass(GitWorkspacePatchMaterializer), "GitWorkspacePatchMaterializer should be a class"
        assert inspect.isclass(MaterializationResult), "MaterializationResult should be a class"
        assert hasattr(Materializer, "__dict__"), "Materializer should be importable"
        assert inspect.isclass(MaterializerRegistry), "MaterializerRegistry should be a class"
        assert callable(create_workspace_materializer), "create_workspace_materializer should be callable"
        assert callable(get_materializer_registry_with_builtins), "builtins registry helper should be callable"
        assert callable(register_materializer), "register_materializer should be callable"


class TestContextImports:
    """Test shepherd-contexts imports."""

    def test_workspace(self) -> None:
        """WorkspaceRef is importable."""
        from shepherd_contexts.workspace import WorkspaceRef

        assert inspect.isclass(WorkspaceRef), "WorkspaceRef should be a class"

    def test_session(self) -> None:
        """SessionState is importable."""
        from shepherd_contexts.session import SessionState

        assert inspect.isclass(SessionState), "SessionState should be a class"

    def test_database(self) -> None:
        """DatabaseContext is importable."""
        from shepherd_contexts.database import DatabaseContext

        assert inspect.isclass(DatabaseContext), "DatabaseContext should be a class"

    def test_database_effect_owner_path(self) -> None:
        """Database effect family should resolve from the contexts owner path."""
        from shepherd_contexts.database.effects import QueryExecuted

        assert inspect.isclass(QueryExecuted), "QueryExecuted should be a class"
        assert QueryExecuted.__module__ == "shepherd_contexts.database.effects"

    def test_context_effect_owner_paths(self) -> None:
        """Remaining context-owned effect families should resolve from contexts owner paths."""
        from shepherd_contexts.mcp.effects import MCPToolCalled
        from shepherd_contexts.session.effects import SessionCreated, SessionForked, SessionResumed
        from shepherd_contexts.workspace.effects import BashCommand, WorkspacePatchCaptured

        assert BashCommand.__module__ == "shepherd_contexts.workspace.effects"
        assert WorkspacePatchCaptured.__module__ == "shepherd_contexts.workspace.effects"
        assert SessionCreated.__module__ == "shepherd_contexts.session.effects"
        assert SessionForked.__module__ == "shepherd_contexts.session.effects"
        assert SessionResumed.__module__ == "shepherd_contexts.session.effects"
        assert MCPToolCalled.__module__ == "shepherd_contexts.mcp.effects"

    def test_mcp(self) -> None:
        """MCPServerContext is importable."""
        from shepherd_contexts.mcp import MCPServerContext

        assert inspect.isclass(MCPServerContext), "MCPServerContext should be a class"

    def test_root_imports(self) -> None:
        """Root package exports all contexts."""
        from shepherd_contexts import (
            DatabaseContext,
            KVStoreContext,
            MCPServerContext,
            SessionState,
            WorkspaceRef,
        )

        assert inspect.isclass(WorkspaceRef), "WorkspaceRef should be a class"
        assert inspect.isclass(SessionState), "SessionState should be a class"
        assert inspect.isclass(DatabaseContext), "DatabaseContext should be a class"
        assert inspect.isclass(MCPServerContext), "MCPServerContext should be a class"
        assert inspect.isclass(KVStoreContext), "KVStoreContext should be a class"


class TestExportOwnerPaths:
    """Test explicit export owner paths."""

    def test_export_package_paths(self) -> None:
        """Public export package is importable."""
        from shepherd_export import from_json, from_trajectory, to_json, to_trajectory
        from shepherd_export.trajectory import ScopeInfo, TrajectoryResult

        assert callable(from_json), "from_json should be callable"
        assert callable(from_trajectory), "from_trajectory should be callable"
        assert callable(to_json), "to_json should be callable"
        assert callable(to_trajectory), "to_trajectory should be callable"
        assert inspect.isclass(ScopeInfo), "ScopeInfo should be a class"
        assert inspect.isclass(TrajectoryResult), "TrajectoryResult should be a class"


class TestDomainImports:
    """Test domain package imports."""

    def test_banking_context(self) -> None:
        """BankingContext is importable."""
        from shepherd_banking import BankingContext

        assert inspect.isclass(BankingContext), "BankingContext should be a class"

    def test_banking_tasks(self) -> None:
        """Banking tasks are importable as function-form callables."""
        from shepherd_banking import query_balance, transfer_funds

        assert callable(transfer_funds), "transfer_funds should be callable"
        assert callable(query_balance), "query_balance should be callable"

    def test_coding_context(self) -> None:
        """GitHubContext is importable."""
        from shepherd_coding import GitHubContext

        assert inspect.isclass(GitHubContext), "GitHubContext should be a class"

    def test_coding_tasks(self) -> None:
        """Coding tasks are importable."""
        from shepherd_coding import FetchPR, ReviewPR, TriagePR

        assert inspect.isclass(FetchPR), "FetchPR should be a class"
        assert inspect.isclass(ReviewPR), "ReviewPR should be a class"
        assert inspect.isclass(TriagePR), "TriagePR should be a class"


class TestMetaPackageImports:
    """Test shepherd meta-package imports."""

    def test_core_reexports(self) -> None:
        """Meta-package re-exports only the Phase 1 callable-spine public surface."""
        import shepherd
        from shepherd import (
            Artifact,
            DeliveryException,
            DeliveryFailed,
            EffectNotPermitted,
            EffectSurfaceEmpty,
            EffectSurfaceTooWide,
            Match,
            OverbroadHandler,
            Permissive,
            Plan,
            PlanNotExtractable,
            Run,
            RunInProgress,
            RunRef,
            Subset,
            Workspace,
            ask,
            current_binding,
            deliver,
            emit_artifact,
            handle,
            task,
            tell,
            workspace,
        )

        # Strict facade-drift guard: exact order + membership. Update deliberately
        # when the public surface changes on purpose (e.g. the P-030 Lane C
        # per-binding grant surface: GitRepo/May/ReadOnly/ReadWrite/Flow/...).
        assert shepherd.__all__ == [
            "__version__",
            "workspace",
            "Workspace",
            "task",
            "deliver",
            "handle",
            "ask",
            "tell",
            "Permissive",
            "current_binding",
            "Run",
            "RunRef",
            "RunInProgress",
            "DeliveryException",
            "DeliveryFailed",
            "emit_artifact",
            "Artifact",
            "Match",
            "Plan",
            "Subset",
            "EffectNotPermitted",
            "EffectSurfaceEmpty",
            "EffectSurfaceTooWide",
            "OverbroadHandler",
            "PlanNotExtractable",
            "GitRepo",
            "GitRepoBasis",
            "open",
            "ShepherdWorkspace",
            "WorkspaceRun",
            "WorkspaceTask",
            "RunOutput",
            "Changeset",
            "ChangesetStat",
            "May",
            "ReadOnly",
            "ReadWrite",
            "GitRepoGrant",
            "Flow",
            "FlowControlClient",
        ]
        assert callable(task), "task should be callable (decorator)"
        assert callable(workspace), "workspace should be callable"
        assert callable(deliver), "deliver should be callable"
        assert callable(ask), "ask should be callable"
        assert callable(tell), "tell should be callable"
        assert callable(handle), "handle should be callable"
        assert callable(current_binding), "current_binding should be callable"
        assert callable(emit_artifact), "emit_artifact should be callable"
        assert inspect.isclass(Workspace), "Workspace should be a class"
        assert inspect.isclass(Match), "Match should be a class"
        assert inspect.isclass(Plan), "Plan should be a class"
        assert inspect.isclass(Subset), "Subset should be a class"
        assert inspect.isclass(Run), "Run should be a class"
        assert inspect.isclass(RunInProgress), "RunInProgress should be a class"
        assert inspect.isclass(RunRef), "RunRef should be a class"
        assert inspect.isclass(DeliveryException), "DeliveryException should be a class"
        assert inspect.isclass(DeliveryFailed), "DeliveryFailed should be a class"
        assert inspect.isclass(EffectNotPermitted), "EffectNotPermitted should be a class"
        assert inspect.isclass(EffectSurfaceEmpty), "EffectSurfaceEmpty should be a class"
        assert inspect.isclass(EffectSurfaceTooWide), "EffectSurfaceTooWide should be a class"
        assert inspect.isclass(OverbroadHandler), "OverbroadHandler should be a class"
        assert inspect.isclass(PlanNotExtractable), "PlanNotExtractable should be a class"
        assert inspect.isclass(Artifact), "Artifact should be a class"
        assert not inspect.isclass(Permissive), "Permissive should be a may= profile instance, not a class"

    def test_meta_package_does_not_export_legacy_runtime_helpers(self) -> None:
        """Legacy helpers remain available from owner modules, not the top-level facade."""
        import shepherd

        for symbol_name in (
            "ShepherdError",
            "ClaudeProvider",
            "DeliveryExhausted",
            "DeliveryLimits",
            "DeliveryStopped",
            "Device",
            "Scope",
            "TaskAdapter",
            "TaskFailed",
            "TaskMetadata",
            "WorkspaceRef",
            "configure",
            "get_messages",
            "mock_steps",
            "reset",
            "scope",
            "task_fn",
        ):
            assert not hasattr(shepherd, symbol_name), f"{symbol_name} should not be exported from shepherd"
            assert symbol_name not in shepherd.__all__, f"{symbol_name} should not be listed in shepherd.__all__"

    def test_delivery_limits_is_runtime_owned_not_top_level(self) -> None:
        """DeliveryLimits remains available from its owner module while semantics are deferred."""
        from shepherd_runtime.nucleus import DeliveryLimits

        import shepherd

        assert not hasattr(shepherd, "DeliveryLimits")
        assert DeliveryLimits(max_turns=1).max_turns == 1

    def test_sync_api_is_not_top_level_after_callable_spine_cut(self) -> None:
        """Legacy sync-first helpers are owner-path or historical APIs, not top-level facade."""
        import shepherd

        for symbol_name in ("configure", "reset", "get_messages", "scope"):
            assert not hasattr(shepherd, symbol_name)
            assert symbol_name not in shepherd.__all__

    def test_meta_package_no_longer_exports_effect_types_registry(self) -> None:
        """The meta-package should not teach the ambient kernel effect registry."""
        module = importlib.reload(importlib.import_module("shepherd"))

        assert not hasattr(module, "EFFECT_TYPES")

    def test_optional_package_owner_imports(self) -> None:
        """Optional packages are imported from owner paths."""
        from shepherd_contexts import WorkspaceRef
        from shepherd_providers import ClaudeProvider

        assert inspect.isclass(ClaudeProvider), "ClaudeProvider should be a class"
        assert inspect.isclass(WorkspaceRef), "WorkspaceRef should be a class"

    def test_context_effect_owner_imports(self) -> None:
        """Context-owned effect families are imported from optional-package owner paths."""
        from shepherd_contexts import (
            BashCommand,
            MCPToolCalled,
            QueryExecuted,
            SessionCreated,
            SessionForked,
            SessionResumed,
            WorkspacePatchCaptured,
        )

        assert inspect.isclass(QueryExecuted), "QueryExecuted should be a class"
        assert QueryExecuted.__module__ == "shepherd_contexts.database.effects"
        assert BashCommand.__module__ == "shepherd_contexts.workspace.effects"
        assert WorkspacePatchCaptured.__module__ == "shepherd_contexts.workspace.effects"
        assert SessionCreated.__module__ == "shepherd_contexts.session.effects"
        assert SessionForked.__module__ == "shepherd_contexts.session.effects"
        assert SessionResumed.__module__ == "shepherd_contexts.session.effects"
        assert MCPToolCalled.__module__ == "shepherd_contexts.mcp.effects"
