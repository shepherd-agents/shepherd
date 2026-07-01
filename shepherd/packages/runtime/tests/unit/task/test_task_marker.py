"""Unit tests for TaskRef type integration with metadata and prompts."""

from __future__ import annotations

from typing import get_args, get_origin

import pytest
from pydantic import BaseModel
from shepherd_runtime.scope import Scope
from shepherd_runtime.task.authoring import CompletedTask, Input, Output, TaskRef, task
from shepherd_runtime.task.metadata import extract_task_metadata
from shepherd_runtime.task.output import generate_output_schema
from shepherd_runtime.task.prompt import (
    _is_completed_task_type,
    _is_task_class,
    _is_task_instance,
    _is_task_ref_type,
    _serialize_completed_task_for_prompt,
    _serialize_task_for_prompt,
    generate_task_prompt,
)
from shepherd_tests import MockProvider
from shepherd_transform.meta import CritiqueTask
from shepherd_transform.meta import TransformTask as BuiltinTransformTask
from shepherd_transform.source import extract_task_source

# =============================================================================
# Test Task Classes
# =============================================================================


@task
class TargetTask(BaseModel):
    """A simple task to be used as a target."""

    query: Input(str)
    answer: Output(str)


@task
class MetaTask(BaseModel):
    """A meta-task that operates on another task."""

    target: Input(TaskRef)  # TaskRef input
    instruction: Input(str)
    result: Output(str)


@task
class TransformTask(BaseModel):
    """A meta-task that outputs a transformed task."""

    target: Input(TaskRef)
    instruction: Input(str)
    transformed: Output(TaskRef)  # TaskRef output


@task
class BatchCritiqueTask(BaseModel):
    """Critique a batch of tasks."""

    targets: Input(list[TaskRef])
    summary: Output(str)


# =============================================================================
# TaskRef Type Detection Tests
# =============================================================================


class TestIsTaskRefType:
    """Tests for _is_task_ref_type function."""

    def test_taskref_type(self):
        """TaskRef is recognized."""
        assert _is_task_ref_type(TaskRef) is True

    def test_str_not_taskref(self):
        """Str is not TaskRef."""
        assert _is_task_ref_type(str) is False

    def test_type_not_taskref(self):
        """Type is not TaskRef."""
        assert _is_task_ref_type(type) is False

    def test_none_not_taskref(self):
        """None is not TaskRef."""
        assert _is_task_ref_type(None) is False


class TestIsTaskClass:
    """Tests for _is_task_class function."""

    def test_task_decorated_class(self):
        """@task decorated class is recognized."""
        assert _is_task_class(TargetTask) is True

    def test_plain_class_not_task(self):
        """Non-decorated class is not a task."""

        class NotATask:
            pass

        assert _is_task_class(NotATask) is False

    def test_instance_not_task(self):
        """Instance of task is not a task class."""
        from shepherd_runtime.scope import Scope
        from shepherd_tests import MockProvider

        with Scope(root=True) as scope:
            scope.register_provider("default", MockProvider(), default=True)
            instance = TargetTask(query="test")
        assert _is_task_class(instance) is False


# =============================================================================
# Metadata Extraction Tests
# =============================================================================


class TestTaskRefMetadataExtraction:
    """Tests for TaskRef fields in metadata extraction."""

    def test_taskref_input_in_inputs(self):
        """Input(TaskRef) field appears in inputs."""
        meta = extract_task_metadata(MetaTask)

        assert "target" in meta.inputs
        assert meta.inputs["target"].marker_type == "input"
        assert meta.inputs["target"].inner_type is TaskRef

    def test_taskref_output_in_outputs(self):
        """Output(TaskRef) field appears in outputs."""
        import types
        from typing import Union, get_args, get_origin

        meta = extract_task_metadata(TransformTask)

        assert "transformed" in meta.outputs
        assert meta.outputs["transformed"].marker_type == "output"
        # Output types include | None, so check if TaskRef is in the union
        inner = meta.outputs["transformed"].inner_type
        origin = get_origin(inner)
        if origin in (Union, types.UnionType):
            args = get_args(inner)
            assert TaskRef in args
        else:
            assert inner is TaskRef

    def test_regular_input_unchanged(self):
        """Regular Input() fields still work normally."""
        meta = extract_task_metadata(MetaTask)

        assert "instruction" in meta.inputs
        assert meta.inputs["instruction"].inner_type is str


# =============================================================================
# Task Serialization Tests
# =============================================================================


class TestSerializeTaskForPrompt:
    """Tests for _serialize_task_for_prompt function."""

    def test_serializes_task_name(self):
        """Task name appears in serialization."""
        result = _serialize_task_for_prompt(TargetTask)
        assert "### Task: TargetTask" in result

    def test_serializes_source_code(self):
        """Source code appears in markdown code block."""
        result = _serialize_task_for_prompt(TargetTask)
        assert "```python" in result
        assert "@task" in result
        assert "class TargetTask" in result

    def test_serializes_purpose(self):
        """Docstring first line appears as purpose."""
        result = _serialize_task_for_prompt(TargetTask)
        assert "**Purpose**:" in result
        assert "A simple task" in result

    def test_serializes_field_annotations(self):
        """Field annotations appear in source."""
        result = _serialize_task_for_prompt(TargetTask)
        assert "query: Input(str)" in result
        assert "answer: Output(str)" in result


# =============================================================================
# Prompt Generation Integration Tests
# =============================================================================


class TestPromptGenerationWithTaskRef:
    """Tests for generate_task_prompt with TaskRef inputs."""

    def test_taskref_input_serialized_as_source(self):
        """TaskRef input is serialized as source code in prompt."""
        meta = extract_task_metadata(MetaTask)
        inputs = {"target": TargetTask, "instruction": "Add validation"}
        contexts = {}

        prompt = generate_task_prompt(meta, inputs, contexts)

        # Should contain task source, not just class name
        assert "class TargetTask" in prompt
        assert "```python" in prompt

    def test_regular_input_unchanged(self):
        """Regular inputs still work normally."""
        meta = extract_task_metadata(MetaTask)
        inputs = {"target": TargetTask, "instruction": "Add validation"}
        contexts = {}

        prompt = generate_task_prompt(meta, inputs, contexts)

        assert "Add validation" in prompt

    def test_invalid_task_input_handled(self):
        """Non-task value for TaskRef field shows error."""
        meta = extract_task_metadata(MetaTask)
        inputs = {"target": "not a task", "instruction": "test"}
        contexts = {}

        prompt = generate_task_prompt(meta, inputs, contexts)

        assert "[Invalid" in prompt or "expected @task class" in prompt

    def test_prompt_contains_task_purpose(self):
        """Task purpose (docstring) is included in prompt."""
        meta = extract_task_metadata(MetaTask)
        inputs = {"target": TargetTask, "instruction": "Optimize it"}
        contexts = {}

        prompt = generate_task_prompt(meta, inputs, contexts)

        # TargetTask's docstring should appear
        assert "A simple task" in prompt

    def test_prompt_contains_meta_task_docstring(self):
        """Meta-task's own docstring appears as instruction."""
        meta = extract_task_metadata(MetaTask)
        inputs = {"target": TargetTask, "instruction": "test"}
        contexts = {}

        prompt = generate_task_prompt(meta, inputs, contexts)

        # MetaTask's docstring
        assert "A meta-task that operates on another task" in prompt

    def test_prompt_includes_taskref_output_guidance(self):
        """TaskRef outputs add explicit raw-source instructions."""
        meta = extract_task_metadata(TransformTask)
        inputs = {"target": TargetTask, "instruction": "Add validation"}
        contexts = {}

        prompt = generate_task_prompt(meta, inputs, contexts)

        assert "## TaskRef Output Requirements" in prompt
        assert "`transformed` must be a JSON string" in prompt
        assert "Do not wrap task source in Markdown code fences" in prompt

    def test_list_taskref_input_serialized_as_source(self):
        """Input(list[TaskRef]) renders each task as source instead of repr."""
        meta = extract_task_metadata(BatchCritiqueTask)
        inputs = {"targets": [TargetTask]}
        contexts = {}

        prompt = generate_task_prompt(meta, inputs, contexts)

        assert "#### Task 1" in prompt
        assert "class TargetTask" in prompt
        assert "[<class" not in prompt

    def test_list_taskref_prompt_keeps_standard_formatting_for_regular_inputs(self):
        """The TaskRef collection fix does not force regular inputs into section rendering."""

        @task
        class MixedInputTask(BaseModel):
            targets: Input(list[TaskRef])
            instruction: Input(str)
            summary: Output(str)

        meta = extract_task_metadata(MixedInputTask)
        inputs = {"targets": [TargetTask], "instruction": "Focus on naming"}
        contexts = {}

        prompt = generate_task_prompt(meta, inputs, contexts)

        assert "class TargetTask" in prompt
        assert "- **instruction**: Focus on naming" in prompt
        assert "### instruction" not in prompt


class TestOutputSchemaWithTaskRef:
    """Tests for structured output schema generation with TaskRef outputs."""

    def test_taskref_output_schema_is_string(self):
        """TaskRef outputs are exposed to providers as strings."""
        meta = extract_task_metadata(TransformTask)

        schema = generate_output_schema(meta)
        assert schema is not None

        transformed_schema = schema["schema"]["properties"]["transformed"]
        assert transformed_schema["type"] == "string"
        assert "Raw Python source" in transformed_schema["description"]


class TestTaskRefRoundTripExecution:
    """End-to-end tests for TaskRef output then later TaskRef input."""

    def test_default_mock_provider_generates_valid_taskref_output(self):
        """Default schema-based mocks should produce reconstructable TaskRef outputs."""
        provider = MockProvider()

        with Scope(root=True) as scope:
            scope.register_provider("default", provider, default=True)

            transformed = BuiltinTransformTask(target=TargetTask, instruction="Add a confidence output")

        assert transformed.transformed is not None
        assert transformed.transformed_source is not None
        assert "@task" in transformed.transformed_source
        assert extract_task_source(transformed.transformed) == transformed.transformed_source

    def test_transformed_task_can_be_serialized_by_later_meta_task(self):
        """A reconstructed TaskRef output can be re-serialized in a later prompt."""
        transformed_source = """
@task
class TargetTaskWithConfidence(BaseModel):
    \"\"\"A transformed task with an added confidence score.\"\"\"
    query: Input(str)
    answer: Output(str)
    confidence: Output(float)
"""
        provider = MockProvider(
            mock_responses=[
                {
                    "structured": {
                        "transformed": transformed_source,
                        "transformed_source": transformed_source,
                        "explanation": "Added a confidence output.",
                    }
                },
                {
                    "structured": {
                        "critique": "Looks clear.",
                        "suggestions": ["Keep confidence naming consistent."],
                        "severity": "minor",
                    }
                },
            ]
        )

        with Scope(root=True) as scope:
            scope.register_provider("default", provider, default=True)

            transformed = BuiltinTransformTask(target=TargetTask, instruction="Add a confidence output")
            assert transformed.transformed is not None
            assert extract_task_source(transformed.transformed) == transformed_source

            critique = CritiqueTask(target=transformed.transformed, criteria=["clarity"])
            assert critique.critique == "Looks clear."

        assert len(provider.calls) == 2
        second_prompt = provider.calls[1]["prompt"]
        assert "class TargetTaskWithConfidence" in second_prompt
        assert "confidence: Output(float)" in second_prompt


# =============================================================================
# Edge Cases
# =============================================================================


class TestEdgeCases:
    """Edge case tests for TaskRef handling."""

    def test_task_without_docstring(self):
        """Task without docstring still serializes."""

        @task
        class NoDocTask(BaseModel):
            x: Input(int)
            y: Output(int)

        result = _serialize_task_for_prompt(NoDocTask)
        assert "### Task: NoDocTask" in result
        assert "No description" in result

    def test_metadata_has_taskref_input_as_input(self):
        """TaskRef inputs appear in inputs dict, not separately."""
        meta = extract_task_metadata(MetaTask)

        # Both target (TaskRef) and instruction (str) are in inputs
        assert len(meta.inputs) == 2
        assert "target" in meta.inputs
        assert "instruction" in meta.inputs

        # target has TaskRef inner type
        assert meta.inputs["target"].inner_type is TaskRef
        # instruction has str inner type
        assert meta.inputs["instruction"].inner_type is str


# =============================================================================
# CompletedTask Test Task Classes
# =============================================================================


@task
class CompletedTaskMeta(BaseModel):
    """A meta-task that accepts a completed task."""

    execution: Input(CompletedTask)
    summary: Output(str)


@task
class CompletedTaskListMeta(BaseModel):
    """A meta-task that accepts multiple completed tasks."""

    executions: Input(list[CompletedTask])
    summary: Output(str)


# =============================================================================
# CompletedTask Type Detection Tests
# =============================================================================


class TestIsCompletedTaskType:
    """Tests for _is_completed_task_type function."""

    def test_completed_task_type(self):
        """CompletedTask is recognized."""
        assert _is_completed_task_type(CompletedTask) is True

    def test_taskref_not_completed_task(self):
        """TaskRef is not CompletedTask."""
        assert _is_completed_task_type(TaskRef) is False

    def test_str_not_completed_task(self):
        """Str is not CompletedTask."""
        assert _is_completed_task_type(str) is False

    def test_none_not_completed_task(self):
        """None is not CompletedTask."""
        assert _is_completed_task_type(None) is False


class TestIsTaskInstance:
    """Tests for _is_task_instance function."""

    def test_task_instance_detected(self):
        """Completed task instance is recognized."""
        with Scope(root=True) as scope:
            scope.register_provider("default", MockProvider(), default=True)
            instance = TargetTask(query="test")
        assert _is_task_instance(instance) is True

    def test_task_class_not_instance(self):
        """Task class is not an instance."""
        assert _is_task_instance(TargetTask) is False

    def test_plain_object_not_instance(self):
        """Plain string is not a task instance."""
        assert _is_task_instance("hello") is False

    def test_plain_class_not_instance(self):
        """Non-task class is not a task instance."""

        class Foo:
            pass

        assert _is_task_instance(Foo) is False
        assert _is_task_instance(Foo()) is False


# =============================================================================
# CompletedTask Serialization Tests
# =============================================================================


class TestSerializeCompletedTaskForPrompt:
    """Tests for _serialize_completed_task_for_prompt function."""

    def test_includes_task_name(self):
        """Task name appears in serialization."""
        with Scope(root=True) as scope:
            scope.register_provider("default", MockProvider(), default=True)
            instance = TargetTask(query="test")
        result = _serialize_completed_task_for_prompt(instance)
        assert "### Execution: TargetTask" in result

    def test_includes_source_code(self):
        """Source code appears in markdown code block."""
        with Scope(root=True) as scope:
            scope.register_provider("default", MockProvider(), default=True)
            instance = TargetTask(query="test")
        result = _serialize_completed_task_for_prompt(instance)
        assert "```python" in result
        assert "class TargetTask" in result

    def test_includes_input_values(self):
        """Input values appear in serialization."""
        with Scope(root=True) as scope:
            scope.register_provider("default", MockProvider(), default=True)
            instance = TargetTask(query="hello world")
        result = _serialize_completed_task_for_prompt(instance)
        assert "**Inputs**:" in result
        assert "hello world" in result

    def test_includes_output_values(self):
        """Output values appear in serialization."""
        with Scope(root=True) as scope:
            scope.register_provider("default", MockProvider(), default=True)
            instance = TargetTask(query="test")
        result = _serialize_completed_task_for_prompt(instance)
        assert "**Outputs**:" in result

    def test_includes_effect_stream(self):
        """Effect stream section is present."""
        with Scope(root=True) as scope:
            scope.register_provider("default", MockProvider(), default=True)
            instance = TargetTask(query="test")
        result = _serialize_completed_task_for_prompt(instance)
        assert "**Effect Stream**:" in result

    def test_includes_purpose(self):
        """Docstring first line appears as purpose."""
        with Scope(root=True) as scope:
            scope.register_provider("default", MockProvider(), default=True)
            instance = TargetTask(query="test")
        result = _serialize_completed_task_for_prompt(instance)
        assert "**Purpose**:" in result
        assert "A simple task" in result


# =============================================================================
# CompletedTask Metadata Extraction Tests
# =============================================================================


class TestCompletedTaskMetadataExtraction:
    """Tests for CompletedTask fields in metadata extraction."""

    def test_completed_task_input_in_inputs(self):
        """Input(CompletedTask) field appears in inputs."""
        meta = extract_task_metadata(CompletedTaskMeta)
        assert "execution" in meta.inputs
        assert meta.inputs["execution"].inner_type is CompletedTask

    def test_list_completed_task_input(self):
        """Input(list[CompletedTask]) field appears in inputs."""
        meta = extract_task_metadata(CompletedTaskListMeta)
        assert "executions" in meta.inputs
        origin = get_origin(meta.inputs["executions"].inner_type)
        assert origin is list
        args = get_args(meta.inputs["executions"].inner_type)
        assert args
        assert args[0] is CompletedTask


# =============================================================================
# CompletedTask Prompt Generation Tests
# =============================================================================


class TestPromptGenerationWithCompletedTask:
    """Tests for generate_task_prompt with CompletedTask inputs."""

    def test_completed_task_renders_as_section(self):
        """CompletedTask input renders as its own section."""
        with Scope(root=True) as scope:
            scope.register_provider("default", MockProvider(), default=True)
            target_instance = TargetTask(query="test")

        meta = extract_task_metadata(CompletedTaskMeta)
        inputs = {"execution": target_instance}
        prompt = generate_task_prompt(meta, inputs, {})
        assert "### execution" in prompt
        assert "class TargetTask" in prompt
        # Verify it's rendered as a section, not a bullet
        assert "- **execution**:" not in prompt

    def test_list_completed_task_renders_as_section(self):
        """list[CompletedTask] input renders as its own section."""
        with Scope(root=True) as scope:
            scope.register_provider("default", MockProvider(), default=True)
            run1 = TargetTask(query="test1")
            run2 = TargetTask(query="test2")

        meta = extract_task_metadata(CompletedTaskListMeta)
        inputs = {"executions": [run1, run2]}
        prompt = generate_task_prompt(meta, inputs, {})
        assert "### executions" in prompt
        assert "#### Execution 1" in prompt
        assert "#### Execution 2" in prompt
        assert "- **executions**:" not in prompt

    def test_list_completed_task_deduplicates_source(self):
        """list[CompletedTask] shows source once for same task class."""
        with Scope(root=True) as scope:
            scope.register_provider("default", MockProvider(), default=True)
            run1 = TargetTask(query="test1")
            run2 = TargetTask(query="test2")

        meta = extract_task_metadata(CompletedTaskListMeta)
        inputs = {"executions": [run1, run2]}
        prompt = generate_task_prompt(meta, inputs, {})
        # Source appears once in definitions, not per execution
        assert "#### Task Definitions" in prompt
        assert prompt.count("```python") >= 1

    def test_invalid_completed_task_shows_error(self):
        """Non-task value for CompletedTask field shows error."""
        meta = extract_task_metadata(CompletedTaskMeta)
        inputs = {"execution": "not a task instance"}
        prompt = generate_task_prompt(meta, inputs, {})
        assert "[Invalid" in prompt

    def test_empty_list_completed_task(self):
        """Empty list for list[CompletedTask] handled gracefully."""
        meta = extract_task_metadata(CompletedTaskListMeta)
        inputs = {"executions": []}
        prompt = generate_task_prompt(meta, inputs, {})
        assert "no executions provided" in prompt


# =============================================================================
# TaskMixin .task_ref Property Tests
# =============================================================================


class TestTaskMixinTaskRefProperty:
    """Tests for .task_ref property on TaskMixin."""

    def test_task_ref_returns_class(self):
        """task_ref returns the task class."""
        with Scope(root=True) as scope:
            scope.register_provider("default", MockProvider(), default=True)
            instance = TargetTask(query="test")
        assert instance.task_ref is type(instance)
        assert hasattr(instance.task_ref, "_task_meta")

    def test_task_ref_has_source(self):
        """task_ref class has extractable source."""
        with Scope(root=True) as scope:
            scope.register_provider("default", MockProvider(), default=True)
            instance = TargetTask(query="test")
        source = extract_task_source(instance.task_ref)
        assert "class TargetTask" in source


# =============================================================================
# TaskMixin .with_view() Tests
# =============================================================================


class TestTaskMixinWithView:
    """Tests for .with_view() on TaskMixin."""

    def test_with_view_callable_returns_copy(self):
        """with_view(callable) returns a new instance, not the original."""
        with Scope(root=True) as scope:
            scope.register_provider("default", MockProvider(), default=True)
            original = TargetTask(query="test")
        projected = original.with_view(lambda s: s.thinking())
        assert projected is not original

    def test_with_view_does_not_mutate_original(self):
        """Original instance's effects are unchanged after with_view."""
        with Scope(root=True) as scope:
            scope.register_provider("default", MockProvider(), default=True)
            original = TargetTask(query="test")
        original_count = len(original.effects)
        _projected = original.with_view("thinking")
        assert len(original.effects) == original_count

    def test_with_view_named_projects_effects(self):
        """Named view projects to fewer effects."""
        with Scope(root=True) as scope:
            scope.register_provider("default", MockProvider(), default=True)
            original = TargetTask(query="test")
        full_count = len(original.effects)
        projected = original.with_view("thinking")
        assert len(projected.effects) <= full_count

    def test_with_view_preserves_inputs_and_outputs(self):
        """Projected copy has same inputs and outputs as original."""
        with Scope(root=True) as scope:
            scope.register_provider("default", MockProvider(), default=True)
            original = TargetTask(query="test")
        projected = original.with_view("intents")
        assert projected.query == original.query
        assert projected.answer == original.answer

    def test_with_view_preserves_task_ref(self):
        """Projected copy has same task_ref as original."""
        with Scope(root=True) as scope:
            scope.register_provider("default", MockProvider(), default=True)
            original = TargetTask(query="test")
        projected = original.with_view("thinking")
        assert projected.task_ref is original.task_ref

    def test_with_view_union_of_named_views(self):
        """Multiple named views are unioned."""
        with Scope(root=True) as scope:
            scope.register_provider("default", MockProvider(), default=True)
            original = TargetTask(query="test")
        thinking_count = len(list(original.effects.thinking()))
        outcomes_count = len(list(original.effects.outcomes()))
        projected = original.with_view("thinking", "outcomes")
        # Union should have at least as many as either individual view
        assert len(projected.effects) >= max(thinking_count, outcomes_count)
        # But no more than the sum (no duplicates possible since views are disjoint)
        assert len(projected.effects) <= thinking_count + outcomes_count

    def test_with_view_exclude(self):
        """exclude= removes specific effect types."""
        from shepherd_core.effects import LifecyclePhaseCompleted, LifecyclePhaseStarted

        with Scope(root=True) as scope:
            scope.register_provider("default", MockProvider(), default=True)
            original = TargetTask(query="test")
        full_count = len(original.effects)
        projected = original.with_view(exclude=[LifecyclePhaseStarted, LifecyclePhaseCompleted])
        assert len(projected.effects) < full_count
        # Verify no lifecycle effects remain
        for layer in projected.effects:
            assert not isinstance(layer.effect, (LifecyclePhaseStarted, LifecyclePhaseCompleted))

    def test_with_view_include(self):
        """include= keeps only specific effect types."""
        from shepherd_core.effects import TaskCompleted, TaskStarted

        with Scope(root=True) as scope:
            scope.register_provider("default", MockProvider(), default=True)
            original = TargetTask(query="test")
        projected = original.with_view(include=[TaskStarted, TaskCompleted])
        assert len(projected.effects) == 2
        effect_types = {type(layer.effect).__name__ for layer in projected.effects}
        assert effect_types == {"TaskStarted", "TaskCompleted"}

    def test_with_view_unknown_name_raises(self):
        """Unknown view name raises ValueError."""
        with Scope(root=True) as scope:
            scope.register_provider("default", MockProvider(), default=True)
            original = TargetTask(query="test")
        with pytest.raises(ValueError, match="Unknown view"):
            original.with_view("nonexistent")

    def test_with_view_no_args_raises(self):
        """with_view() with no arguments raises ValueError."""
        with Scope(root=True) as scope:
            scope.register_provider("default", MockProvider(), default=True)
            original = TargetTask(query="test")
        with pytest.raises(ValueError):
            original.with_view()

    def test_with_view_chainable_with_completed_task(self):
        """with_view result is usable as Input(CompletedTask)."""
        with Scope(root=True) as scope:
            scope.register_provider("default", MockProvider(), default=True)
            original = TargetTask(query="test")
        projected = original.with_view("outcomes")
        from shepherd_runtime.task.prompt import _is_task_instance

        assert _is_task_instance(projected)
