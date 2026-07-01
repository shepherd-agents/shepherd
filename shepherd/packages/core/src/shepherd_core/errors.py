"""Comprehensive error hierarchy for the shepherd framework.

All errors inherit from ShepherdError for easy catching.
Each error carries context-specific attributes for debugging.

Error categories by lifecycle phase:
    - Configure phase: ConfigurationError, BindingValidationError
    - Prepare phase: PreparationError, RollbackError
    - Execute phase: ExecutionError, CapabilityError, ProviderNotFoundError
    - Capture phase: CaptureError, ArtifactNotFoundError
    - Task/Step: ContextResolutionError, OutputValidationError, StepExecutionError
"""

from __future__ import annotations

import traceback
from typing import TYPE_CHECKING, Any

from shepherd_core.text import smart_truncate

if TYPE_CHECKING:
    from pathlib import Path

    from shepherd_core.scope.stream import Stream
    from shepherd_core.types import ExecutionResult, ProviderCapabilities


# =============================================================================
# Base Error
# =============================================================================


class ShepherdError(Exception):
    """Base class for all shepherd framework errors.

    Catch this to handle any framework error:

        try:
            async with ExecutionLifecycle(...) as lc:
                await lc.execute(prompt)
        except ShepherdError as e:
            print(f"Framework error: {e}")

    All errors include a debug_hint property pointing to debugging tools.
    """

    @property
    def debug_hint(self) -> str:
        """Hint pointing to debugging tools.

        Returns a string suggesting debugging commands. This is included
        automatically in SDKExecutionError output, but can be accessed
        directly on any ShepherdError.

        Example:
            try:
                result = MyTask(...)
            except ShepherdError as e:
                print(e)
                print(e.debug_hint)
        """
        return "Debug: scope.effects.debug_summary() | session.debug_info()"


# =============================================================================
# Scope / Containment Errors
# =============================================================================


class ContainmentError(ShepherdError):
    """Error related to effect containment violations.

    Raised when an operation violates the containment model, such as:
    - Trying to materialize a discarded scope
    - Trying to merge a materialized scope

    See design/effect-system/GLOSSARY.md for the containment model.
    """


class MaterializationError(ShepherdError):
    """Raised when materialization fails in an expected way.

    This error is used to distinguish expected materialization failures
    (e.g., a registered materializer returns success=False) from unexpected
    exceptions thrown during the materialization process.

    When rollback also fails, both the original error and rollback failures
    are captured for complete debugging context.

    Attributes:
        original_error: The exception that triggered materialization failure
        rollback_errors: Tuple of (effect_type, error_message) for failed rollbacks
        message: Human-readable description of the failure
    """

    def __init__(
        self,
        message: str,
        original_error: Exception | None = None,
        rollback_errors: tuple[tuple[str, str], ...] = (),
    ):
        self.original_error = original_error
        self.rollback_errors = rollback_errors
        super().__init__(message)

    def __str__(self) -> str:
        lines = [super().__str__()]
        if self.original_error:
            lines.append(f"  Original error: {type(self.original_error).__name__}: {self.original_error}")
        if self.rollback_errors:
            lines.append(f"  Rollback failures ({len(self.rollback_errors)}):")
            for effect_type, error_msg in self.rollback_errors:
                lines.append(f"    - {effect_type}: {error_msg}")
        return "\n".join(lines)


# =============================================================================
# Configure Phase Errors
# =============================================================================


class ScopeNotConfiguredError(ShepherdError):
    """No scope available for task execution.

    Raised when a task is executed without a configured scope.
    This typically means either:
    1. no ambient ``shepherd.workspace(...)`` is active for function-form tasks
    2. the task was not executed within an owner-path
       ``shepherd_runtime.scope.Scope`` block

    Attributes:
        task_name: The task that attempted to execute (if known)
        hint: Suggestion for how to fix the issue
    """

    def __init__(
        self,
        message: str,
        task_name: str | None = None,
        hint: str | None = None,
    ):
        self.task_name = task_name
        self.hint = hint
        super().__init__(message)


class ConfigurationError(ShepherdError):
    """Error during configure phase (pure phase, no side effects yet).

    This error is raised when context.configure() fails. Since configure
    is a pure operation, no cleanup is needed when this error occurs.
    """

    def __init__(self, context_id: str, message: str):
        self.context_id = context_id
        super().__init__(f"Configuration failed for {context_id}: {message}")


class SessionCWDMismatchError(ConfigurationError):
    """Session resumption failed due to CWD mismatch.

    The Claude CLI requires that session resumption (with fork_session=True)
    uses the same working directory as the original session. This error is
    raised when the composed binding's CWD differs from the session's original CWD.

    Common causes:
    - A task with SessionState context lacks the WorkspaceRef context that was
      present when the session was created
    - Multiple tasks in a workflow use different workspace paths

    Solution: Ensure all tasks that resume a session include the same workspace
    context (or explicitly set the same CWD).

    Attributes:
        session_cwd: The CWD where the session was originally created
        binding_cwd: The CWD in the current composed binding
    """

    def __init__(self, session_cwd: str, binding_cwd: str):
        self.session_cwd = session_cwd
        self.binding_cwd = binding_cwd
        message = (
            f"Cannot resume session: CWD mismatch.\n"
            f"  Session was created with CWD: {session_cwd}\n"
            f"  Current task CWD: {binding_cwd}\n"
            f"Hint: Ensure all tasks that resume a session include the same workspace context."
        )
        # Call ShepherdError.__init__ directly to avoid ConfigurationError's formatting
        ShepherdError.__init__(self, message)


class BindingNotFoundError(KeyError, ShepherdError):
    """Context binding not found in scope.

    A hybrid error that inherits from both KeyError (for compatibility with
    code that catches KeyError) and ShepherdError (for framework-specific handling).

    Raised when attempting to access a binding that doesn't exist in the scope
    or any parent scope.

    Attributes:
        binding_name: The name that was requested
        available_bindings: List of bindings that are available (if known)
    """

    def __init__(
        self,
        binding_name: str,
        available_bindings: list[str] | None = None,
    ):
        self.binding_name = binding_name
        self.available_bindings = available_bindings or []
        message = f"Context '{binding_name}' not bound"
        if self.available_bindings:
            message += f". Available: {sorted(self.available_bindings)}"
        # Call both parent __init__ methods
        KeyError.__init__(self, binding_name)
        ShepherdError.__init__(self, message)

    def __str__(self) -> str:
        # Override to provide clean message (KeyError adds quotes)
        message = f"Context '{self.binding_name}' not bound"
        if self.available_bindings:
            message += f". Available: {sorted(self.available_bindings)}"
        return message


class BindingValidationError(ShepherdError):
    """Provider cannot satisfy context's binding requirements.

    Raised by Provider.validate_binding() during configure phase,
    before any side effects occur. This allows early failure with
    clear error messages about what the provider cannot support.

    Attributes:
        context_id: The context that produced the unsatisfied binding
        unsatisfied_requirements: List of requirements the provider cannot meet
        provider_capabilities: The capabilities the provider declared
    """

    def __init__(
        self,
        context_id: str,
        unsatisfied_requirements: list[str],
        provider_capabilities: ProviderCapabilities | None = None,
    ):
        self.context_id = context_id
        self.unsatisfied_requirements = unsatisfied_requirements
        self.provider_capabilities = provider_capabilities

        reqs = "\n    - ".join(unsatisfied_requirements)
        super().__init__(
            f"Provider cannot satisfy binding requirements:\n"
            f"  Context: {context_id}\n"
            f"  Unsatisfied requirements:\n    - {reqs}"
        )


# =============================================================================
# Prepare Phase Errors
# =============================================================================


class PreparationError(ShepherdError):
    """Error during prepare phase.

    When preparation fails, the ExecutionLifecycle will automatically
    roll back any contexts that were successfully prepared before the
    failure.

    Attributes:
        context_id: The context whose preparation failed
        cause: The underlying exception (if any)
        contexts_prepared: Context IDs that were prepared before failure
    """

    def __init__(
        self,
        context_id: str,
        message: str,
        cause: Exception | None = None,
        contexts_prepared: list[str] | None = None,
    ):
        self.context_id = context_id
        self.cause = cause
        self.contexts_prepared = contexts_prepared or []
        super().__init__(f"Preparation failed for {context_id}: {message}")


class RollbackError(ShepherdError):
    """Wrapper when cleanup fails during preparation rollback.

    This error is raised when a PreparationError occurs AND one or more
    cleanup operations fail during rollback. It wraps both the original
    error and the cleanup failures to provide complete context.

    Attributes:
        original_error: The PreparationError that triggered rollback
        cleanup_failures: List of (context_id, exception) pairs
    """

    def __init__(
        self,
        original_error: PreparationError,
        cleanup_failures: list[tuple[str, Exception]],
    ):
        self.original_error = original_error
        self.cleanup_failures = cleanup_failures

        failures = "\n    - ".join(f"{cid}: {err}" for cid, err in cleanup_failures)
        super().__init__(
            f"{original_error}\n"
            f"Additionally, {len(cleanup_failures)} cleanup(s) failed during rollback:\n"
            f"    - {failures}"
        )


# =============================================================================
# Execute Phase Errors
# =============================================================================


class ExecutionError(ShepherdError):
    """Error during execute phase.

    Raised when the provider's SDK execution fails. May include
    a partial result if some work was completed before the failure.

    Attributes:
        partial_result: Any partial result from the execution (may be None)
        suggestions: Actionable suggestions for fixing the error
    """

    def __init__(
        self,
        message: str,
        partial_result: ExecutionResult | None = None,
        suggestions: list[str] | None = None,
    ):
        self.partial_result = partial_result
        self.suggestions = suggestions or []
        super().__init__(message)

    def __str__(self) -> str:
        base = super().__str__()
        if not self.suggestions:
            return base
        suggestions_text = "\n  - ".join(self.suggestions)
        return f"{base}\n\nSuggestions:\n  - {suggestions_text}"


class SDKExecutionError(ExecutionError):
    """SDK execution failed with captured context.

    This error wraps SDK failures and captures rich debugging context
    including the execution phase, session ID, last tool called, stderr,
    and actionable suggestions.

    Attributes:
        original_error: The underlying exception from the SDK
        stderr: Captured stderr output (if available)
        stdout: Captured stdout output (if available)
        prompt_preview: Truncated preview of the prompt sent
        sdk_options: Options dict passed to the SDK
        session_id: Current session ID (for session-based providers)
        last_tool_name: Name of the last tool called before failure
        last_tool_params: Parameters of the last tool call
        phase: Lifecycle phase where the error occurred
        error_traceback: Formatted traceback string
    """

    def __init__(
        self,
        message: str,
        *,
        original_error: Exception,
        stderr: str | None = None,
        stdout: str | None = None,
        prompt_preview: str = "",
        sdk_options: dict[str, Any] | None = None,
        session_id: str | None = None,
        last_tool_name: str | None = None,
        last_tool_params: dict[str, Any] | None = None,
        phase: str = "execute",
        suggestions: list[str] | None = None,
        capture_traceback: bool = True,
    ):
        self.original_error = original_error
        self.stderr = stderr
        self.stdout = stdout
        self.prompt_preview = smart_truncate(prompt_preview, max_len=200)
        self.sdk_options = sdk_options or {}
        self.session_id = session_id
        self.last_tool_name = last_tool_name
        self.last_tool_params = last_tool_params or {}
        self.phase = phase

        # Capture traceback from original error
        if capture_traceback and original_error.__traceback__:
            tb_lines = traceback.format_exception(
                type(original_error),
                original_error,
                original_error.__traceback__,
            )
            self.error_traceback = "".join(tb_lines[-5:])  # Last 5 lines
        else:
            self.error_traceback = ""

        super().__init__(message, suggestions=suggestions)

    def __str__(self) -> str:
        lines = [
            f"SDKExecutionError during {self.phase} phase",
            f"  Error: {type(self.original_error).__name__}: {self.original_error}",
        ]

        if self.session_id:
            lines.append(f"  Session: {self.session_id}")

        if self.last_tool_name:
            params_preview = smart_truncate(str(self.last_tool_params), max_len=100)
            lines.append(f"  Last tool: {self.last_tool_name}({params_preview})")

        if self.stderr:
            stderr_preview = smart_truncate(self.stderr.strip(), max_len=300)
            lines.append(f"  Stderr: {stderr_preview}")

        if self.error_traceback:
            lines.append(f"  Traceback (most recent):\n{self.error_traceback}")

        if self.suggestions:
            lines.append("  Suggestions:")
            for s in self.suggestions:
                lines.append(f"    - {s}")

        lines.append("")
        lines.append("  Hint: Call stream.debug_summary() for full execution timeline")

        return "\n".join(lines)


class TaskExecutionError(ShepherdError):
    """Task execution failed with captured effects.

    This error wraps task failures and captures the effect stream up to
    the point of failure, enabling debugging via effect inspection.

    The effect stream is captured BEFORE the exception is raised, so it
    contains all effects including the TaskFailed effect with debugging
    context.

    Attributes:
        task_name: Name of the task that failed
        phase: Lifecycle phase where failure occurred
        effects: The effect stream captured at failure (for debugging)
        suggestions: Actionable suggestions for fixing the error
        cause: The underlying exception that caused the failure

    Example:
        try:
            result = WriteCode(feature="auth", filename="auth.py")
        except TaskExecutionError as e:
            print(f"Task '{e.task_name}' failed during {e.phase} phase")
            print(f"Effects captured: {len(e.effects)}")

            # Query the effects for debugging
            for tc in e.effects.query(ToolCallCompleted):
                print(f"  Tool: {tc.tool_name}")

            # Get detailed failure info from TaskFailed effect
            for failed in e.effects.query(TaskFailed):
                print(f"  Last tool: {failed.last_tool_name}")
                if failed.suggestions:
                    print(f"  Suggestions: {', '.join(failed.suggestions)}")
    """

    def __init__(
        self,
        message: str,
        *,
        task_name: str,
        phase: str = "execute",
        effects: Stream | None = None,
        suggestions: tuple[str, ...] = (),
        cause: Exception | None = None,
    ):
        self.task_name = task_name
        self.phase = phase
        self.effects = effects
        self.suggestions = suggestions
        self.cause = cause
        super().__init__(message)

    def __str__(self) -> str:
        lines = [
            f"TaskExecutionError: Task '{self.task_name}' failed during {self.phase} phase",
            f"  {self.args[0] if self.args else 'Unknown error'}",
        ]

        if self.cause:
            lines.append(f"  Cause: {type(self.cause).__name__}: {self.cause}")

        if self.effects is not None:
            lines.append(f"  Effects captured: {len(self.effects)}")

        if self.suggestions:
            lines.append("  Suggestions:")
            for s in self.suggestions:
                lines.append(f"    - {s}")

        lines.append("")
        lines.append("  Hint: Inspect error.effects for the effect stream leading to failure")
        lines.append("  Debug: error.effects.debug_summary() for execution timeline")

        return "\n".join(lines)


class CapabilityError(ShepherdError):
    """Tool blocked due to missing capability.

    Raised when a tool requires a capability that the context doesn't
    provide. This is a security mechanism to prevent unauthorized
    operations.

    Attributes:
        tool_name: Name of the blocked tool
        required_capability: The capability the tool requires
        context_id: The context that lacks the capability
        available_capabilities: Capabilities the context does have
    """

    def __init__(
        self,
        tool_name: str,
        required_capability: str,
        context_id: str,
        available_capabilities: frozenset[str] | None = None,
    ):
        self.tool_name = tool_name
        self.required_capability = required_capability
        self.context_id = context_id
        self.available_capabilities = available_capabilities or frozenset()
        super().__init__(
            f"Tool '{tool_name}' requires capability '{required_capability}' "
            f"not available in context '{context_id}'. "
            f"Has: {sorted(self.available_capabilities)}"
        )


class ProviderNotFoundError(KeyError, ShepherdError):
    """Referenced provider not found in scope's registry.

    A hybrid error that inherits from both KeyError (for compatibility with
    code that catches KeyError) and ShepherdError (for framework-specific handling).

    Raised when attempting to use a provider by name that hasn't
    been registered with the scope.

    Attributes:
        provider_name: The name that was requested
        available_providers: List of registered provider names
    """

    def __init__(self, provider_name: str, available_providers: list[str] | None = None):
        self.provider_name = provider_name
        self.available_providers = available_providers or []
        message = f"Provider '{provider_name}' not registered"
        if self.available_providers:
            message += f". Available: {sorted(self.available_providers)}"
        # Call both parent __init__ methods
        KeyError.__init__(self, provider_name)
        ShepherdError.__init__(self, message)

    def __str__(self) -> str:
        # Override to provide clean message (KeyError adds quotes)
        message = f"Provider '{self.provider_name}' not registered"
        if self.available_providers:
            message += f". Available: {sorted(self.available_providers)}"
        return message


# =============================================================================
# Capture Phase Errors
# =============================================================================


class CaptureError(ShepherdError):
    """Error during capture phase.

    Raised when context.capture() fails after successful execution.
    The execution result may still be available even if capture fails.

    Attributes:
        context_id: The context whose capture failed
        cause: The underlying exception (if any)
    """

    def __init__(
        self,
        context_id: str,
        message: str,
        cause: Exception | None = None,
    ):
        self.context_id = context_id
        self.cause = cause
        super().__init__(f"Capture failed for {context_id}: {message}")


class ArtifactNotFoundError(ShepherdError):
    """Required artifact was not written by agent.

    Raised when a task declares a required artifact (required=True,
    the default) but the LLM fails to create the file in the
    .artifacts/ directory.

    Attributes:
        filename: The expected filename in artifacts directory
        expected_path: Full path where the file was expected
        field_name: The output field that expected this artifact
    """

    def __init__(
        self,
        filename: str,
        expected_path: str | Path,
        field_name: str,
    ):
        self.filename = filename
        self.expected_path = str(expected_path)
        self.field_name = field_name
        super().__init__(f"Artifact '{filename}' for field '{field_name}' not created. Expected at: {expected_path}")


# =============================================================================
# Task/Step Errors
# =============================================================================


class ContextResolutionError(ShepherdError):
    """Cannot resolve context for a task field.

    Raised when a task declares a Context[T] field but no context
    of the required type is bound in the scope.

    Attributes:
        field_name: The task field that needs a context
        expected_type: The type of context expected
        available_contexts: List of (name, type) pairs for bound contexts
    """

    def __init__(
        self,
        field_name: str,
        expected_type: type,
        available_contexts: list[tuple[str, type]] | None = None,
    ):
        self.field_name = field_name
        self.expected_type = expected_type
        self.available_contexts = available_contexts or []

        if self.available_contexts:
            available = "\n    - ".join(f'"{n}": {t.__name__}' for n, t in self.available_contexts)
            msg = (
                f"Cannot resolve context for field '{field_name}' "
                f"(type: {expected_type.__name__})\n"
                f"  Available contexts:\n    - {available}"
            )
        else:
            msg = f"Cannot resolve context for field '{field_name}' (type: {expected_type.__name__}): no contexts bound"
        super().__init__(msg)


class CheckFailedError(ShepherdError):
    """A declarative Check on an Input or Output field failed.

    Raised when a ``Check(predicate)`` marker on a task field evaluates to
    ``False`` at execution time.

    Attributes:
        task_name: Name of the task whose check failed
        field_name: The field that failed validation
        value: The value that was checked
        check: The ``Check`` instance that failed
        phase: ``"precondition"`` or ``"postcondition"``
    """

    def __init__(
        self,
        task_name: str,
        field_name: str,
        value: Any,
        check: Any,
        phase: str,
    ):
        self.task_name = task_name
        self.field_name = field_name
        self.value = value
        self.check = check
        self.phase = phase
        msg = check.format_message(value, field_name)
        super().__init__(f"{phase} check failed for {task_name}.{field_name}: {msg}")


class OutputValidationError(ShepherdError):
    """Structured output failed validation.

    Raised when the LLM's structured output doesn't match the
    expected type for an output field.

    Attributes:
        field: The output field that failed validation
        expected_type: The type that was expected
        actual_value: The value that was received
    """

    def __init__(self, field: str, expected_type: type, actual_value: Any):
        self.field = field
        self.expected_type = expected_type
        self.actual_value = actual_value
        super().__init__(
            f"Output validation failed for field '{field}': "
            f"expected {expected_type.__name__}, got {type(actual_value).__name__}"
        )


class TaskRefOutputError(ShepherdError):
    """TaskRef output failed type validation or reconstruction.

    Raised when an output field declared as `Output(TaskRef)` does not contain
    a raw Python source string, or when that source cannot be reconstructed
    into a valid `@task` class.

    Attributes:
        field: The output field name
        reason: Human-readable explanation of the failure
        actual_value: The original value received from structured output, if any
    """

    def __init__(self, field: str, reason: str, actual_value: Any = None):
        self.field = field
        self.reason = reason
        self.actual_value = actual_value
        super().__init__(f"TaskRef output failed for field '{field}': {reason}")


class StepExecutionError(ShepherdError):
    """Error during @step execution within a composite task.

    Wraps errors that occur during step execution to provide
    context about which step failed and in which parent task.

    Attributes:
        step_name: Name of the step method that failed
        parent_task: Name of the task containing the step
        cause: The underlying exception
    """

    def __init__(self, step_name: str, parent_task: str, cause: Exception):
        self.step_name = step_name
        self.parent_task = parent_task
        self.cause = cause
        super().__init__(f"Step '{parent_task}:{step_name}' failed: {cause}")


class StepOutputError(ShepherdError):
    """Error parsing or validating step output.

    Raised when the LLM's response for a @step cannot be parsed
    into the expected return type.

    Attributes:
        step_name: Name of the step that produced the output
        expected_type: The expected return type
        received: The actual value received
        reason: Explanation of why the error occurred
    """

    def __init__(
        self,
        step_name: str,
        expected_type: type | None = None,
        received: Any = None,
        reason: str = "",
    ):
        self.step_name = step_name
        self.expected_type = expected_type
        self.received = received
        self.reason = reason

        # Build message
        type_name = getattr(expected_type, "__name__", str(expected_type)) if expected_type else "unknown"
        message = f"Step '{step_name}' output error: expected {type_name}, got {received!r}. {reason}"
        super().__init__(message)


class SchemaGenerationError(ShepherdError):
    """Error generating JSON schema for task outputs.

    Raised when output schema generation fails, typically due to
    conflicting type definitions (e.g., two output fields with
    nested classes of the same name but different structures).

    Attributes:
        message: Description of the schema generation error
        conflicting_key: The $defs key that has conflicting definitions (if applicable)
        field_name: The output field being processed when error occurred (if applicable)
    """

    def __init__(
        self,
        message: str,
        conflicting_key: str | None = None,
        field_name: str | None = None,
    ):
        self.conflicting_key = conflicting_key
        self.field_name = field_name
        super().__init__(message)


# =============================================================================
# Strict Mode Errors
# =============================================================================


class MetadataExtractionError(ShepherdError):
    """Failed to extract type hints from a @task class.

    Raised in strict mode when get_type_hints() fails. This usually indicates
    forward references that can't be resolved, which may cause Input/Output/Context
    fields to be missed.

    Attributes:
        class_name: Name of the task class
        cause: The underlying exception from get_type_hints()
    """

    def __init__(self, class_name: str, cause: Exception):
        self.class_name = class_name
        self.cause = cause
        super().__init__(
            f"Failed to extract type hints from '{class_name}': {cause}\n"
            f"This may cause Input/Output/Context fields to be missed.\n"
            f"To use raw annotations instead, disable strict mode:\n"
            f"  from shepherd_core import set_strict_mode\n"
            f"  set_strict_mode(False)"
        )


class PluginLoadError(ShepherdError):
    """Failed to load a plugin during discovery.

    Raised in strict mode when an entry point fails to load. This ensures
    users are aware that their plugin didn't load correctly.

    Attributes:
        plugin_name: Name of the plugin entry point
        plugin_group: The entry point group (e.g., "shepherd.providers")
        cause: The underlying exception from ep.load()
    """

    def __init__(self, plugin_name: str, plugin_group: str, cause: Exception):
        self.plugin_name = plugin_name
        self.plugin_group = plugin_group
        self.cause = cause
        super().__init__(
            f"Failed to load plugin '{plugin_name}' from group '{plugin_group}': {cause}\n"
            f"To skip broken plugins instead of failing, disable strict mode:\n"
            f"  from shepherd_core import set_strict_mode\n"
            f"  set_strict_mode(False)"
        )


class SandboxSnapshotError(ShepherdError):
    """Failed to read a file during sandbox snapshot capture.

    Raised in strict mode when a file cannot be read during base snapshot
    creation. This could cause changes to that file to go undetected.

    Attributes:
        file_path: Path to the unreadable file
        sandbox_path: Root path of the sandbox
        cause: The underlying exception (OSError, IOError, etc.)
    """

    def __init__(self, file_path: str, sandbox_path: str, cause: Exception):
        self.file_path = file_path
        self.sandbox_path = sandbox_path
        self.cause = cause
        super().__init__(
            f"Cannot read file during sandbox snapshot: {file_path}\n"
            f"Changes to this file may go undetected.\n"
            f"Cause: {cause}\n"
            f"To skip unreadable files instead, disable strict mode:\n"
            f"  from shepherd_core import set_strict_mode\n"
            f"  set_strict_mode(False)"
        )


# =============================================================================
# Exports
# =============================================================================

__all__ = [
    "ArtifactNotFoundError",
    "BindingNotFoundError",
    "BindingValidationError",
    "CapabilityError",
    # Capture phase
    "CaptureError",
    "CheckFailedError",
    "ConfigurationError",
    # Scope / Containment
    "ContainmentError",
    # Task/Step
    "ContextResolutionError",
    # Execute phase
    "ExecutionError",
    "MaterializationError",
    # Strict mode
    "MetadataExtractionError",
    "OutputValidationError",
    "PluginLoadError",
    # Prepare phase
    "PreparationError",
    "ProviderNotFoundError",
    "RollbackError",
    "SDKExecutionError",
    "SandboxSnapshotError",
    "SchemaGenerationError",
    # Configure phase
    "ScopeNotConfiguredError",
    # Base
    "ShepherdError",
    "StepExecutionError",
    "StepOutputError",
    "TaskExecutionError",
    "TaskRefOutputError",
]
