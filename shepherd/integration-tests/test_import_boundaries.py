"""Cross-package import-boundary contracts for extracted owner paths."""

from __future__ import annotations

import ast
import sys
from pathlib import Path

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parents[2] / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from _workspace_layout import integration_tests_dir, package_dir, require_workspace_root

WORKSPACE_ROOT = require_workspace_root(Path(__file__))
INTEGRATION_TESTS_DIR = integration_tests_dir(WORKSPACE_ROOT)
if INTEGRATION_TESTS_DIR is None:
    raise RuntimeError("Could not resolve the integration-tests directory for this workspace layout.")

DOWNSTREAM_PACKAGES = (
    "shepherd-runtime",
    "shepherd-transform",
    "shepherd-sandboxes",
    "shepherd",
    "shepherd-banking",
    "shepherd-coding",
    "shepherd-contexts",
    "shepherd-export",
    "shepherd-providers",
    "shepherd-tests",
)

SCANNED_DIRS = ("src", "tests")
RUNTIME_PACKAGE_DIR = package_dir(WORKSPACE_ROOT, "shepherd-runtime")
TRANSFORM_PACKAGE_DIR = package_dir(WORKSPACE_ROOT, "shepherd-transform")
CORE_PACKAGE_DIR = package_dir(WORKSPACE_ROOT, "shepherd-core")
EXPORT_PACKAGE_DIR = package_dir(WORKSPACE_ROOT, "shepherd-export")
RUNTIME_SRC_DIR = RUNTIME_PACKAGE_DIR / "src" / "shepherd_runtime"
RUNTIME_SCOPE_CLUSTER_DIR = RUNTIME_SRC_DIR / "_scope"
RUNTIME_SCOPE_SUBSTRATE_HUB = RUNTIME_SCOPE_CLUSTER_DIR / "substrate.py"
EXPORT_SRC_DIR = EXPORT_PACKAGE_DIR / "src" / "shepherd_export"
# Files outside _scope/* that are allowed to import shepherd_core.scope substrate
# directly.  Every other runtime file must route through one of these gateways
# or through _scope/substrate.py.
RUNTIME_SUBSTRATE_GATEWAY_ALLOWLIST = {
    RUNTIME_SRC_DIR / "_persistence_layers.py",
    RUNTIME_SRC_DIR / "scope_types.py",
}
CORE_SCOPE_SUBSTRATE_PREFIXES = (
    "shepherd_core.scope.context_ref",
    "shepherd_core.scope.model",
    "shepherd_core.scope.stream",
    "shepherd_core.scope.types",
)
CORE_SCOPE_INTERNAL_DIR = CORE_PACKAGE_DIR / "src" / "shepherd_core" / "scope"
CORE_SCOPE_PUBLIC_COMPAT_ALLOWLIST = {
    INTEGRATION_TESTS_DIR / "test_package_imports.py",
    CORE_PACKAGE_DIR / "src" / "shepherd_core" / "__init__.py",
    CORE_PACKAGE_DIR / "src" / "shepherd_core" / "scope" / "__init__.py",
    CORE_PACKAGE_DIR / "tests" / "unit" / "test_run.py",
    CORE_PACKAGE_DIR / "tests" / "unit" / "test_task_discovery.py",
}

FORBIDDEN_PREFIXES = (
    "shepherd_core.cache",
    "shepherd_core.context.runtime",
    "shepherd_core.export",
    "shepherd_core.handlers",
    "shepherd_core.grounding",
    "shepherd_core.step",
    "shepherd_core.testing",
    "shepherd_core.meta",
    "shepherd_core.combinators",
    "shepherd_core.lifecycle",
    "shepherd_core.persistence",
    "shepherd_core.task",
    "shepherd_core._shared.registry",
    "shepherd_core.context.sandbox",
    "shepherd_core.device.container",
    "shepherd_core.device.errors",
    "shepherd_core.device.local",
    "shepherd_core.device.transfer",
    "shepherd_core.device.container.effect_collector",
    "shepherd_core.device.container.context_registry",
    "shepherd_core.device.container.device",
    "shepherd_core.device.container.provider_registry",
    "shepherd_core.device.container.task_runner",
    "shepherd_core.device.container.overlay_extractor",
    "shepherd_core.device.container.podman",
    "shepherd_core.device.container.preflight",
    "shepherd_core.device.container.vm_extraction",
    "shepherd_core.device.container.vm_paths",
    "shepherd_core.device.daytona",
    "shepherd_core.device.e2b",
    "shepherd_core.device.kubernetes",
    "shepherd_core.device.modal",
    "shepherd_core.device.prime",
    "shepherd_core.scope.scope",
    "shepherd_core._error_patterns",
    "shepherd_core._truncation",
)

RUNTIME_FORBIDDEN_PREFIXES = (
    "shepherd_core._shared.coerce",
    "shepherd_core._shared.mock_value",
    "shepherd_core._shared.registry",
    "shepherd_core._shared.schema",
    "shepherd_core.combinators",
    "shepherd_core.context.runtime",
    "shepherd_core.context.sandbox",
    "shepherd_core.grounding",
    "shepherd_core.handlers",
    "shepherd_core.lifecycle",
    "shepherd_core.meta",
    "shepherd_core.task",
    "shepherd_core.step",
    "shepherd_core.device.container",
    "shepherd_core.device.errors",
    "shepherd_core.device.local",
    "shepherd_core.device.daytona",
    "shepherd_core.device.e2b",
    "shepherd_core.device.kubernetes",
    "shepherd_core.device.modal",
    "shepherd_core.device.prime",
    "shepherd_core.device.transfer",
    "shepherd_core.scope._binding_registry",
    "shepherd_core.scope._bindings",
    "shepherd_core.scope._bootstrap",
    "shepherd_core.scope._checkpoint",
    "shepherd_core.scope._effect_materialization",
    "shepherd_core.scope._emission",
    "shepherd_core.scope._hierarchy",
    "shepherd_core.scope._hosts",
    "shepherd_core.scope._inspection",
    "shepherd_core.scope._materialization",
    "shepherd_core.scope._persistence",
    "shepherd_core.scope._provider_registry",
    "shepherd_core.scope._resume",
    "shepherd_core.scope._sandbox",
    "shepherd_core.scope.checkpoint",
    "shepherd_core.scope.runtime",
    "shepherd_core.scope.materialization",
    "shepherd_core.scope.scope",
    "shepherd_core._shared.logging",
    "shepherd_core._truncation",
    "shepherd_core.cache",
    "shepherd_core.persistence",
)

RUNTIME_ALLOWED_CORE_PREFIXES = (
    "shepherd_core.config",
    "shepherd_core.context.kernel",
    "shepherd_core.effects",
    "shepherd_core.errors",
    "shepherd_core.foundation",
    "shepherd_core.output",
    "shepherd_core.provider",
    "shepherd_core.schema",
    "shepherd_core.scope.context_ref",
    "shepherd_core.scope.model",
    "shepherd_core.scope.stream",
    "shepherd_core.scope.types",
    "shepherd_core.text",
    "shepherd_core.types",
)
RUNTIME_CONTRACTION_DEBT_PREFIXES: tuple[str, ...] = ()
LEGACY_RUNTIME_TRANSFORM_PREFIXES = (
    "shepherd_runtime.task.chaining",
    "shepherd_runtime.task.source",
    "shepherd_runtime.task.transform_lock",
)
LEGACY_TRANSFORM_PUBLIC_PREFIXES = ("shepherd_transform.reconstruction",)
LEGACY_RUNTIME_TRANSFORM_IMPORTS = {
    "shepherd_runtime.task.secure": {"ReconstructionResult", "safe_reconstruct"},
}
LEGACY_PRIVATE_TASK_SOURCE_PREFIXES = (
    "shepherd_runtime.task._source_extraction",
    "shepherd_runtime.task._source_validation",
)
LEGACY_PRIVATE_TASK_RECONSTRUCTION_PREFIXES = ("shepherd_runtime.task._task_reconstruction",)
TASK_SOURCE_GATEWAY_PREFIXES = (
    "shepherd_runtime.task.source_analysis",
    "shepherd_runtime.task.source_validation",
)
TASK_SOURCE_GATEWAY_ALLOWLIST = {
    RUNTIME_SRC_DIR / "_cache_key.py",
    RUNTIME_SRC_DIR / "_lifecycle_impl.py",
    RUNTIME_SRC_DIR / "device" / "container" / "programmatic_execution.py",
    RUNTIME_SRC_DIR / "task" / "reconstruction.py",
    RUNTIME_SRC_DIR / "task" / "_secure_impl.py",
    RUNTIME_SRC_DIR / "task" / "_task_reconstruction.py",
    RUNTIME_SRC_DIR / "task" / "output.py",
    RUNTIME_SRC_DIR / "task" / "prompt.py",
    TRANSFORM_PACKAGE_DIR / "src" / "shepherd_transform" / "source.py",
    RUNTIME_PACKAGE_DIR / "tests" / "unit" / "lifecycle" / "test_spike4_e2e_mock_device.py",
}
TASK_RECONSTRUCTION_GATEWAY_PREFIXES = ("shepherd_runtime.task.reconstruction",)
TASK_RECONSTRUCTION_GATEWAY_ALLOWLIST = {
    RUNTIME_SRC_DIR / "device" / "container" / "programmatic_execution.py",
    RUNTIME_SRC_DIR / "task" / "_secure_impl.py",
    RUNTIME_SRC_DIR / "task" / "_task_reconstruction.py",
    TRANSFORM_PACKAGE_DIR / "src" / "shepherd_transform" / "source.py",
    RUNTIME_PACKAGE_DIR / "tests" / "unit" / "lifecycle" / "test_spike4_e2e_mock_device.py",
    RUNTIME_PACKAGE_DIR / "tests" / "unit" / "task" / "test_spike3_task_runner_branch.py",
}
DOWNSTREAM_IMPORT_ALLOWLIST = {
    RUNTIME_PACKAGE_DIR / "tests" / "unit" / "task" / "test_autoconfig.py",
    RUNTIME_PACKAGE_DIR / "tests" / "unit" / "task" / "test_spike2_serialization_roundtrip.py",
}
RUNTIME_CORE_IMPORT_ALLOWLIST = {
    RUNTIME_PACKAGE_DIR / "tests" / "unit" / "task" / "test_autoconfig.py",
}


def _matches_forbidden_prefix(module_name: str, forbidden_prefixes: tuple[str, ...]) -> bool:
    return any(module_name == prefix or module_name.startswith(f"{prefix}.") for prefix in forbidden_prefixes)


def _iter_python_files(package_name: str) -> list[Path]:
    package_root = package_dir(WORKSPACE_ROOT, package_name)
    python_files: list[Path] = []
    for directory_name in SCANNED_DIRS:
        directory = package_root / directory_name
        if directory.exists():
            python_files.extend(sorted(directory.rglob("*.py")))
    return python_files


def _iter_boundary_scanned_files() -> list[Path]:
    python_files: list[Path] = []
    for package_name in (*DOWNSTREAM_PACKAGES, "shepherd-core"):
        python_files.extend(_iter_python_files(package_name))
    python_files.extend(sorted(INTEGRATION_TESTS_DIR.rglob("*.py")))
    return python_files


def _scan_file(path: Path, forbidden_prefixes: tuple[str, ...]) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    violations: list[str] = []

    for node in ast.walk(tree):
        module_names: list[str] = []
        if isinstance(node, ast.Import):
            module_names = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module is not None:
            module_names = [node.module]

        for module_name in module_names:
            if _matches_forbidden_prefix(module_name, forbidden_prefixes):
                relative_path = path.relative_to(WORKSPACE_ROOT)
                violations.append(f"{relative_path}:{node.lineno}: {module_name}")

    return violations


def _scan_imported_modules(path: Path, package_prefix: str) -> list[tuple[int, str]]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    imports: list[tuple[int, str]] = []

    for node in ast.walk(tree):
        module_names: list[str] = []
        if isinstance(node, ast.Import):
            module_names = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module is not None:
            module_names = [node.module]

        for module_name in module_names:
            if module_name == package_prefix or module_name.startswith(f"{package_prefix}."):
                imports.append((node.lineno, module_name))

    return imports


def _scan_calls_missing_keyword(path: Path, function_names: set[str], keyword_name: str) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    violations: list[str] = []

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue

        function_name: str | None = None
        if isinstance(node.func, ast.Name):
            function_name = node.func.id
        elif isinstance(node.func, ast.Attribute):
            function_name = node.func.attr

        if function_name not in function_names:
            continue

        if any(keyword.arg == keyword_name for keyword in node.keywords):
            continue

        relative_path = path.relative_to(WORKSPACE_ROOT)
        violations.append(f"{relative_path}:{node.lineno}: {function_name}()")

    return violations


def _scan_attribute_access(path: Path, attribute_names: set[str]) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    violations: list[str] = []

    for node in ast.walk(tree):
        if not isinstance(node, ast.Attribute):
            continue
        if node.attr not in attribute_names:
            continue
        relative_path = path.relative_to(WORKSPACE_ROOT)
        violations.append(f"{relative_path}:{node.lineno}: .{node.attr}")

    return violations


def _scan_legacy_symbol_imports(path: Path, legacy_imports: dict[str, set[str]]) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    violations: list[str] = []

    for node in ast.walk(tree):
        if not isinstance(node, ast.ImportFrom) or node.level != 0 or node.module is None:
            continue
        forbidden_names = legacy_imports.get(node.module)
        if not forbidden_names:
            continue
        imported_names = {alias.name for alias in node.names}
        matched_names = sorted(imported_names & forbidden_names)
        if matched_names:
            relative_path = path.relative_to(WORKSPACE_ROOT)
            violations.append(f"{relative_path}:{node.lineno}: {node.module}::{', '.join(matched_names)}")

    return violations


def _scan_symbol_imports(path: Path, forbidden_symbols: dict[str, set[str]]) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    violations: list[str] = []

    for node in ast.walk(tree):
        if not isinstance(node, ast.ImportFrom) or node.level != 0 or node.module is None:
            continue

        forbidden_names = forbidden_symbols.get(node.module)
        if not forbidden_names:
            continue

        matched_names = sorted({alias.name for alias in node.names} & forbidden_names)
        if not matched_names:
            continue

        relative_path = path.relative_to(WORKSPACE_ROOT)
        violations.append(f"{relative_path}:{node.lineno}: {node.module} imports {', '.join(matched_names)}")

    return violations


def _is_core_scope_internal_path(path: Path) -> bool:
    try:
        path.relative_to(CORE_SCOPE_INTERNAL_DIR)
    except ValueError:
        return False
    return True


def _scan_public_scope_compat_imports(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    violations: list[str] = []
    runtime_root_names = {
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
    }
    runtime_scope_names = {"Scope", "ScopeProxy", "current_scope", "require_scope"}
    runtime_handler_names = {
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
    }
    runtime_materialization_names = {
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
    }
    runtime_device_root_names = {
        "BundleApplicationError",
        "ContainerDevice",
        "ContainerSandbox",
        "ContainerStartupError",
        "Device",
        "DeviceBoundaryError",
        "DeviceNestingError",
        "DeviceSpaceError",
        "EffectCollector",
        "EffectExtractionError",
        "LocalDevice",
        "MountError",
        "OverlayEffectExtractor",
        "OverlayMount",
        "PatchApplicationError",
        "PodmanSandboxManager",
        "ProviderCreationError",
        "TaskTimeoutError",
        "TransferBundle",
        "collect_visible_patches",
        "compute_content_hash",
        "create_provider",
        "deserialize_context",
        "get_current_device",
        "get_device",
        "list_devices",
        "register_context_deserializer",
        "register_device",
        "register_provider_factory",
    }

    for node in ast.walk(tree):
        if not isinstance(node, ast.ImportFrom) or node.level != 0 or node.module is None:
            continue
        if node.module not in {"shepherd_core", "shepherd_core.scope", "shepherd_core.device"}:
            continue

        imported_names = {alias.name for alias in node.names}
        compat_names: set[str] = set()
        if node.module == "shepherd_core":
            compat_names |= imported_names & runtime_root_names
            compat_names |= imported_names & runtime_handler_names
            compat_names |= imported_names & runtime_scope_names
        if node.module == "shepherd_core.device":
            compat_names |= imported_names & runtime_device_root_names
        if node.module in {"shepherd_core", "shepherd_core.scope"}:
            compat_names |= imported_names & runtime_materialization_names
        if not compat_names:
            continue

        relative_path = path.relative_to(WORKSPACE_ROOT)
        joined_names = ", ".join(sorted(compat_names))
        violations.append(f"{relative_path}:{node.lineno}: {node.module} imports {joined_names}")

    return violations


def _scan_public_context_compat_imports(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    violations: list[str] = []
    runtime_context_names = {
        "Bindable",
        "BindableContext",
        "GitWorktreeSandbox",
        "NullSandbox",
        "OrphanedWorktreeRegistry",
        "Sandbox",
    }
    runtime_context_protocol_names = {
        "Bindable",
        "BindableContext",
        "NullSandbox",
        "RuntimeContextDefaults",
        "Sandbox",
    }

    for node in ast.walk(tree):
        if not isinstance(node, ast.ImportFrom) or node.level != 0 or node.module is None:
            continue
        if node.module not in {"shepherd_core.context", "shepherd_core.context.protocol"}:
            continue

        imported_names = {alias.name for alias in node.names}
        compat_names: set[str] = set()
        if node.module == "shepherd_core.context":
            compat_names |= imported_names & runtime_context_names
        if node.module == "shepherd_core.context.protocol":
            compat_names |= imported_names & runtime_context_protocol_names
        if not compat_names:
            continue

        relative_path = path.relative_to(WORKSPACE_ROOT)
        joined_names = ", ".join(sorted(compat_names))
        violations.append(f"{relative_path}:{node.lineno}: {node.module} imports {joined_names}")

    return violations


@pytest.mark.parametrize("package_name", DOWNSTREAM_PACKAGES)
def test_downstream_packages_avoid_private_or_moved_shepherd_core_imports(package_name: str) -> None:
    """Downstream packages should use owner paths instead of private or migrated core modules."""
    violations: list[str] = []
    for path in _iter_python_files(package_name):
        if path in DOWNSTREAM_IMPORT_ALLOWLIST:
            continue
        forbidden_prefixes = RUNTIME_FORBIDDEN_PREFIXES if package_name == "shepherd-runtime" else FORBIDDEN_PREFIXES
        violations.extend(_scan_file(path, forbidden_prefixes))

    assert not violations, (
        f"{package_name} imports private or migrated shepherd_core modules.\n"
        "Use owner paths such as shepherd_export, shepherd_tests, or shepherd_runtime.* instead.\n" + "\n".join(violations)
    )


def test_repo_avoids_core_private_scope_helper_imports() -> None:
    """Moved scope helpers should stay deleted from repo-wide import surfaces."""
    violations: list[str] = []
    for path in _iter_boundary_scanned_files():
        if _is_core_scope_internal_path(path):
            continue
        violations.extend(_scan_file(path, RUNTIME_FORBIDDEN_PREFIXES))

    scope_private_violations = [
        v for v in violations if "shepherd_core.scope._" in v or "shepherd_core.scope.runtime" in v
    ]
    assert not scope_private_violations, (
        "Core-private scope helpers should not be imported outside shepherd_core.scope internals.\n"
        "Use shepherd_runtime._scope.*, shepherd_runtime.session, shepherd_runtime.execution, "
        "or public shepherd_runtime.scope instead.\n" + "\n".join(scope_private_violations)
    )


def test_repo_prefers_runtime_scope_owner_path() -> None:
    """Repo callers should not import runtime-owned APIs from core scope barrels."""
    violations: list[str] = []
    for path in _iter_boundary_scanned_files():
        if path in CORE_SCOPE_PUBLIC_COMPAT_ALLOWLIST:
            continue
        violations.extend(_scan_public_scope_compat_imports(path))

    assert not violations, (
        "Import lifecycle APIs from shepherd_runtime.lifecycle, task APIs from shepherd_runtime.task.*, "
        "step APIs from shepherd_runtime.step.api, combinators from shepherd_runtime.combinators, "
        "Scope-family runtime APIs from shepherd_runtime.scope, and context-materialization APIs "
        "from shepherd_runtime.materialization. Import handler "
        "APIs from shepherd_runtime.handlers. Import device APIs from shepherd_runtime.device* rather "
        "than shepherd_core.device. Do not import those runtime-owned names from shepherd_core, "
        "shepherd_core.scope, or shepherd_core.device.\n" + "\n".join(violations)
    )


def test_repo_prefers_runtime_context_owner_paths() -> None:
    """Repo callers should not treat core context compat exports as owner paths."""
    violations: list[str] = []
    for path in _iter_boundary_scanned_files():
        if path == INTEGRATION_TESTS_DIR / "test_package_imports.py":
            continue
        try:
            path.relative_to(CORE_PACKAGE_DIR)
            continue
        except ValueError:
            pass
        violations.extend(_scan_public_context_compat_imports(path))

    assert not violations, (
        "Import runtime-facing context helpers from shepherd_runtime.context or "
        "shepherd_runtime.context.sandbox rather than from shepherd_core.context "
        "kernel barrels. Kernel lifecycle protocol imports may still come "
        "from shepherd_core.context or shepherd_core.context.kernel.\n" + "\n".join(violations)
    )


def test_repo_avoids_core_context_runtime_compat_module_outside_core() -> None:
    """`shepherd_core.context.runtime` should not be used as a first-class owner path."""
    violations: list[str] = []

    for path in _iter_boundary_scanned_files():
        if path == INTEGRATION_TESTS_DIR / "test_import_boundaries.py":
            continue
        try:
            path.relative_to(CORE_PACKAGE_DIR)
            continue
        except ValueError:
            pass

        relative_path = path.relative_to(WORKSPACE_ROOT)
        for lineno, module_name in _scan_imported_modules(path, "shepherd_core.context.runtime"):
            violations.append(f"{relative_path}:{lineno}: {module_name}")

    assert not violations, (
        "Do not import runtime-facing context helpers from `shepherd_core.context.runtime` "
        "anywhere in the repo.\n"
        "Use `shepherd_runtime.context` instead.\n" + "\n".join(violations)
    )


def test_repo_avoids_kernel_ambient_effect_registration_outside_core() -> None:
    """Moved packages should not depend on kernel ambient-registration globals."""
    forbidden_symbols = {
        "shepherd_core": {"EFFECT_TYPES", "register_effect"},
        "shepherd_core.effects": {"EFFECT_TYPES", "register_effect"},
    }
    violations: list[str] = []

    for path in _iter_boundary_scanned_files():
        try:
            path.relative_to(CORE_PACKAGE_DIR)
            continue
        except ValueError:
            pass
        violations.extend(_scan_symbol_imports(path, forbidden_symbols))

    assert not violations, (
        "Outside `shepherd-core`, moved effect families must use explicit runtime "
        "contributor mappings rather than importing `register_effect` or "
        "`EFFECT_TYPES` from core.\n" + "\n".join(violations)
    )


def test_repo_avoids_legacy_runtime_transform_task_owner_paths() -> None:
    """Repo callers should import transform orchestration from shepherd_transform."""
    violations: list[str] = []
    for path in _iter_boundary_scanned_files():
        violations.extend(_scan_file(path, LEGACY_RUNTIME_TRANSFORM_PREFIXES))

    assert not violations, (
        "Import transformation chaining, source manipulation, and transform locks from "
        "shepherd_transform, not from the removed legacy runtime task submodules.\n" + "\n".join(violations)
    )


def test_repo_avoids_removed_transform_reconstruction_module() -> None:
    """Repo callers should import transform reconstruction from shepherd_transform.source."""
    violations: list[str] = []
    for path in _iter_boundary_scanned_files():
        violations.extend(_scan_file(path, LEGACY_TRANSFORM_PUBLIC_PREFIXES))

    assert not violations, (
        "Import transform reconstruction helpers from shepherd_transform.source, not from the "
        "removed shepherd_transform.reconstruction module.\n" + "\n".join(violations)
    )


def test_repo_avoids_legacy_runtime_transform_symbol_imports() -> None:
    """Repo callers should import transform reconstruction facade from shepherd_transform."""
    violations: list[str] = []
    for path in _iter_boundary_scanned_files():
        if path == INTEGRATION_TESTS_DIR / "test_import_boundaries.py":
            continue
        violations.extend(_scan_legacy_symbol_imports(path, LEGACY_RUNTIME_TRANSFORM_IMPORTS))

    assert not violations, (
        "Import transform reconstruction helpers from shepherd_transform.source, not from "
        "shepherd_runtime.task.secure.\n" + "\n".join(violations)
    )


def test_runtime_and_transform_avoid_private_task_source_modules() -> None:
    """Shared task-source helpers should no longer be imported from private modules."""
    violations: list[str] = []

    for package_name in ("shepherd-runtime", "shepherd-transform"):
        for path in _iter_python_files(package_name):
            violations.extend(_scan_file(path, LEGACY_PRIVATE_TASK_SOURCE_PREFIXES))

    assert not violations, (
        "Import shared task source-analysis and validation helpers from "
        "`shepherd_runtime.task.source_analysis` / `shepherd_runtime.task.source_validation`, "
        "not from the legacy private `_source_*` modules.\n" + "\n".join(violations)
    )


def test_transform_avoids_private_runtime_reconstruction_module() -> None:
    """Transform should import non-secure reconstruction from the explicit runtime seam."""
    violations: list[str] = []

    for path in _iter_python_files("shepherd-transform"):
        violations.extend(_scan_file(path, LEGACY_PRIVATE_TASK_RECONSTRUCTION_PREFIXES))

    assert not violations, (
        "Import non-secure reconstruction from `shepherd_runtime.task.reconstruction`, not from "
        "`shepherd_runtime.task._task_reconstruction`.\n" + "\n".join(violations)
    )


def test_runtime_core_imports_are_classified_as_kernel_or_explicit_debt() -> None:
    """`shepherd_runtime` should only depend on kernel-safe core imports or named debt."""
    unclassified: list[str] = []

    for path in _iter_python_files("shepherd-runtime"):
        if path in RUNTIME_CORE_IMPORT_ALLOWLIST:
            continue
        relative_path = path.relative_to(WORKSPACE_ROOT)
        allowed_prefixes = RUNTIME_ALLOWED_CORE_PREFIXES
        for lineno, module_name in _scan_imported_modules(path, "shepherd_core"):
            if _matches_forbidden_prefix(module_name, RUNTIME_FORBIDDEN_PREFIXES):
                continue
            if _matches_forbidden_prefix(module_name, allowed_prefixes):
                continue
            if _matches_forbidden_prefix(module_name, RUNTIME_CONTRACTION_DEBT_PREFIXES):
                continue
            unclassified.append(f"{relative_path}:{lineno}: {module_name}")

    assert not unclassified, (
        "`shepherd_runtime` imports from `shepherd_core` must be either kernel-safe dependencies "
        "or explicitly named contraction debt.\n"
        "The context split is now reduced to `shepherd_core.context.kernel`; broad "
        "`shepherd_core.context` imports should not reappear in runtime.\n"
        "Classify new imports in `RUNTIME_ALLOWED_CORE_PREFIXES` or "
        "`RUNTIME_CONTRACTION_DEBT_PREFIXES`, or reroute them to runtime owners.\n" + "\n".join(unclassified)
    )


def test_runtime_checkpoint_import_is_fully_hard_cut_from_core() -> None:
    """Runtime must not statically import the removed core checkpoint module."""
    violations: list[str] = []

    for path in _iter_python_files("shepherd-runtime"):
        relative_path = path.relative_to(WORKSPACE_ROOT)
        for lineno, module_name in _scan_imported_modules(path, "shepherd_core.scope.checkpoint"):
            violations.append(f"{relative_path}:{lineno}: {module_name}")

    assert not violations, (
        "`shepherd_runtime` should not import `shepherd_core.scope.checkpoint` anywhere.\n"
        "Checkpoint is now a runtime-owned hard cut, not a path-scoped gateway to a core "
        "implementation.\n" + "\n".join(violations)
    )


def test_scope_cluster_imports_substrate_only_through_hub() -> None:
    """Files under ``_scope/`` must import core scope substrate only via ``substrate.py``.

    This enforces the concentration achieved in P0C-0 PR 1: the fourteen
    ``_scope/*`` implementation files no longer import
    ``shepherd_core.scope.{context_ref,model,stream,types}`` directly.  Only the
    dedicated hub at ``_scope/substrate.py`` may do so.

    See ``P0C-0-SCOPE-IMPORT-CONTRACTION-PLAN.md`` (PR 1.5) for rationale.
    """
    violations: list[str] = []

    for path in sorted(RUNTIME_SCOPE_CLUSTER_DIR.rglob("*.py")):
        if path == RUNTIME_SCOPE_SUBSTRATE_HUB:
            continue
        for lineno, module_name in _scan_imported_modules(path, "shepherd_core"):
            if _matches_forbidden_prefix(module_name, CORE_SCOPE_SUBSTRATE_PREFIXES):
                relative_path = path.relative_to(WORKSPACE_ROOT)
                violations.append(f"{relative_path}:{lineno}: {module_name}")

    assert not violations, (
        "Files under `shepherd_runtime._scope` must import core scope substrate "
        "types through `_scope/substrate.py`, not directly from "
        "`shepherd_core.scope.*`.\n"
        "Move the import to `_scope/substrate.py` and re-import from there.\n" + "\n".join(violations)
    )


def test_runtime_substrate_imports_use_gateway_files_only() -> None:
    """Runtime files outside ``_scope/`` may only import core scope substrate from gateway files.

    After P0C-0 PRs 1-3, the only runtime files outside ``_scope/*`` that may
    import ``shepherd_core.scope.{context_ref,model,stream,types}`` directly are:

    - ``_persistence_layers.py`` — EffectLayer codec seam
    - ``scope_types.py`` — boundary-facing protocol layer
    Everything else must route through ``_scope/substrate.py``, ``scope_types.py``,
    ``_persistence_layers.py``, or ``create_stream()``.

    See ``P0C-0-SCOPE-IMPORT-CONTRACTION-PLAN.md`` (PR 4) for rationale.
    """
    violations: list[str] = []

    for path in sorted(RUNTIME_SRC_DIR.rglob("*.py")):
        # Skip _scope/* cluster — enforced separately by the hub test above
        try:
            path.relative_to(RUNTIME_SCOPE_CLUSTER_DIR)
            continue
        except ValueError:
            pass

        # Skip gateway files
        if path in RUNTIME_SUBSTRATE_GATEWAY_ALLOWLIST:
            continue

        for lineno, module_name in _scan_imported_modules(path, "shepherd_core"):
            if _matches_forbidden_prefix(module_name, CORE_SCOPE_SUBSTRATE_PREFIXES):
                relative_path = path.relative_to(WORKSPACE_ROOT)
                violations.append(f"{relative_path}:{lineno}: {module_name}")

    assert not violations, (
        "Runtime files outside `_scope/` must import core scope substrate "
        "types only from gateway files (`_persistence_layers.py`, "
        "`scope_types.py`) or via `_scope/substrate.py`.\n"
        "Do not add direct `shepherd_core.scope.*` imports to peripheral runtime "
        "files.\n" + "\n".join(violations)
    )


def test_runtime_codec_decode_calls_thread_registry_explicitly() -> None:
    """Runtime-owned codec seams must not fall back to ambient decode globals."""
    codec_checks = {
        RUNTIME_SRC_DIR / "_persistence_layers.py": {"decode_effect"},
        RUNTIME_SRC_DIR / "_persistence_writer.py": {"layer_from_dict"},
        RUNTIME_SRC_DIR / "device" / "container" / "effect_collector.py": {"decode_effect"},
    }

    violations: list[str] = []
    for path, function_names in codec_checks.items():
        violations.extend(_scan_calls_missing_keyword(path, function_names, "registry"))

    assert not violations, (
        "Runtime-owned codec helpers must thread an explicit `registry=` argument "
        "through effect decode calls instead of relying on ambient globals.\n" + "\n".join(violations)
    )


def test_runtime_and_export_surfaces_route_decode_through_local_helpers() -> None:
    """Boundary surfaces should not deserialize effects inline."""
    direct_decode_imports = {"shepherd_core.effects": {"effect_from_dict"}}
    surface_files = (
        RUNTIME_SRC_DIR / "_persistence_layers.py",
        RUNTIME_SRC_DIR / "device" / "container" / "effect_collector.py",
        EXPORT_SRC_DIR / "json_export.py",
        EXPORT_SRC_DIR / "trajectory.py",
    )

    violations: list[str] = []
    for path in surface_files:
        violations.extend(_scan_symbol_imports(path, direct_decode_imports))

    assert not violations, (
        "Runtime/export boundary surfaces should route effect decode through their "
        "package-local codec helpers instead of importing `effect_from_dict` inline.\n" + "\n".join(violations)
    )


def test_export_codec_decode_calls_thread_registry_explicitly() -> None:
    """Export-owned codec helpers must thread explicit registries through decode."""
    violations = _scan_calls_missing_keyword(
        EXPORT_SRC_DIR / "_effect_codec.py",
        {"effect_from_dict"},
        "registry",
    )

    assert not violations, (
        "Export-owned codec helpers must thread an explicit `registry=` argument "
        "through effect decode calls instead of relying on ambient globals.\n" + "\n".join(violations)
    )


def test_runtime_avoids_kernel_provider_slot_access() -> None:
    """Runtime provider ownership should not regress to kernel scope slots."""
    attribute_names = {"_providers", "_default_provider"}
    violations: list[str] = []

    for path in _iter_python_files("shepherd-runtime"):
        violations.extend(_scan_attribute_access(path, attribute_names))

    assert not violations, (
        "Runtime provider state is runtime-owned; do not read or write kernel "
        "scope provider slots like `._providers` or `._default_provider`.\n" + "\n".join(violations)
    )


def test_task_source_gateways_are_imported_only_from_allowlisted_files() -> None:
    """Explicit task-source gateways must stay internal to approved runtime/transform files."""
    violations: list[str] = []

    for path in _iter_boundary_scanned_files():
        if path in TASK_SOURCE_GATEWAY_ALLOWLIST:
            continue
        for lineno, module_name in _scan_imported_modules(path, "shepherd_runtime.task"):
            if _matches_forbidden_prefix(module_name, TASK_SOURCE_GATEWAY_PREFIXES):
                relative_path = path.relative_to(WORKSPACE_ROOT)
                violations.append(f"{relative_path}:{lineno}: {module_name}")

    assert not violations, (
        "Explicit task-source gateways are internal runtime seams. General repo callers should "
        "continue to use `shepherd_transform.source` or `shepherd_runtime.task.secure`, not "
        "`shepherd_runtime.task.source_analysis` / `shepherd_runtime.task.source_validation`.\n" + "\n".join(violations)
    )


def test_task_reconstruction_gateway_is_imported_only_from_allowlisted_files() -> None:
    """Explicit non-secure reconstruction gateway must stay internal to approved files."""
    violations: list[str] = []

    for path in _iter_boundary_scanned_files():
        if path in TASK_RECONSTRUCTION_GATEWAY_ALLOWLIST:
            continue
        for lineno, module_name in _scan_imported_modules(path, "shepherd_runtime.task"):
            if _matches_forbidden_prefix(module_name, TASK_RECONSTRUCTION_GATEWAY_PREFIXES):
                relative_path = path.relative_to(WORKSPACE_ROOT)
                violations.append(f"{relative_path}:{lineno}: {module_name}")

    assert not violations, (
        "Explicit non-secure runtime reconstruction is an internal seam. General repo callers "
        "should continue to use `shepherd_transform.source` or `shepherd_runtime.task.secure`, "
        "not `shepherd_runtime.task.reconstruction`.\n" + "\n".join(violations)
    )
