"""Tests for @task decorator execution functionality.

These tests verify:
1. Task metadata extraction
2. Context resolution from scope
3. Prompt generation
4. Mock mode execution
5. Integration with ExecutionLifecycle
"""

from dataclasses import dataclass
from typing import Literal

import pytest
from pydantic import BaseModel
from shepherd_core.context import ExecutionContextDefaults
from shepherd_core.errors import ContextResolutionError
from shepherd_core.types import ExecutionResult, ReversibilityLevel
from shepherd_runtime.scope import Scope
from shepherd_runtime.task.authoring import Artifact, Context, Input, Output, task
from shepherd_runtime.task.metadata import extract_task_metadata, resolve_contexts
from shepherd_runtime.task.output import extract_outputs
from shepherd_runtime.task.prompt import generate_task_prompt
from shepherd_tests import MockProvider

# =============================================================================
# Test Fixtures - Simple Contexts
# =============================================================================


@dataclass
class SimpleContext(ExecutionContextDefaults):
    """Simple test context for unit tests."""

    name: str
    value: int = 0

    @property
    def context_id(self) -> str:
        return f"simple:{self.name}"

    @property
    def reversibility(self) -> ReversibilityLevel:
        return ReversibilityLevel.AUTO

    def __str__(self) -> str:
        return f"SimpleContext(name={self.name}, value={self.value})"


@dataclass
class InvisibleContext(ExecutionContextDefaults):
    """Context that is invisible in prompts."""

    session_id: str

    @property
    def context_id(self) -> str:
        return f"session:{self.session_id}"

    @property
    def reversibility(self) -> ReversibilityLevel:
        return ReversibilityLevel.AUTO

    def __str__(self) -> str:
        return ""  # Invisible


# =============================================================================
# Test Tasks
# =============================================================================


@task
class SimpleTask(BaseModel):
    """A simple task for testing."""

    text: Input(str)
    result: Output(str)


@task(guidance="Be concise and helpful")
class TaskWithGuidance(BaseModel):
    """A task with additional guidance."""

    query: Input(str)
    answer: Output(str)


@task
class TaskWithContext(BaseModel):
    """A task with a context field."""

    query: Input(str)
    ctx: Context(SimpleContext)
    answer: Output(str)


@task
class TaskWithMultipleContexts(BaseModel):
    """A task with multiple context fields."""

    query: Input(str)
    primary: Context(SimpleContext)
    session: Context(InvisibleContext)
    answer: Output(str)


@task
class TaskWithArtifact(BaseModel):
    """A task that produces an artifact."""

    topic: Input(str)
    summary: Output(str)
    document: Artifact(str, filename="doc.md")


@task
class TaskWithMultipleOutputs(BaseModel):
    """A task with multiple output fields."""

    text: Input(str)
    summary: Output(str)
    word_count: Output(int)
    sentiment: Output(Literal["positive", "negative", "neutral"])


# =============================================================================
# Test: Metadata Extraction
# =============================================================================


class TestMetadataExtraction:
    """Tests for extract_task_metadata()."""

    def test_extracts_basic_fields(self) -> None:
        """Test extraction of input/output fields."""
        meta = extract_task_metadata(SimpleTask)

        assert meta.name == "SimpleTask"
        assert "text" in meta.inputs
        assert "result" in meta.outputs
        assert meta.inputs["text"].marker_type == "input"
        assert meta.outputs["result"].marker_type == "output"

    def test_extracts_docstring(self) -> None:
        """Test docstring extraction."""
        meta = extract_task_metadata(SimpleTask)
        assert "simple task" in meta.docstring.lower()

    def test_extracts_guidance(self) -> None:
        """Test guidance extraction from decorator."""
        meta = TaskWithGuidance._task_meta
        assert meta.guidance == "Be concise and helpful"

    def test_extracts_context_fields(self) -> None:
        """Test extraction of context fields."""
        meta = extract_task_metadata(TaskWithContext)

        assert "ctx" in meta.contexts
        assert meta.contexts["ctx"].inner_type == SimpleContext

    def test_extracts_artifact_fields(self) -> None:
        """Test extraction of artifact fields."""
        meta = extract_task_metadata(TaskWithArtifact)

        assert "document" in meta.artifacts
        assert "document" in meta.artifact_markers
        assert meta.artifact_markers["document"].filename == "doc.md"

    def test_extracts_multiple_outputs(self) -> None:
        """Test extraction of multiple output fields."""
        meta = extract_task_metadata(TaskWithMultipleOutputs)

        assert len(meta.outputs) == 3
        assert "summary" in meta.outputs
        assert "word_count" in meta.outputs
        assert "sentiment" in meta.outputs


# =============================================================================
# Test: Prompt Generation
# =============================================================================


class TestPromptGeneration:
    """Tests for generate_task_prompt()."""

    def test_includes_docstring(self) -> None:
        """Test that prompt includes task docstring."""
        meta = extract_task_metadata(SimpleTask)
        prompt = generate_task_prompt(meta, {"text": "hello"}, {})

        assert "simple task" in prompt.lower()

    def test_includes_inputs(self) -> None:
        """Test that prompt includes input values."""
        meta = extract_task_metadata(SimpleTask)
        prompt = generate_task_prompt(meta, {"text": "hello world"}, {})

        assert "text" in prompt
        assert "hello world" in prompt

    def test_includes_visible_context(self) -> None:
        """Test that prompt includes visible context descriptions."""
        meta = extract_task_metadata(TaskWithContext)
        ctx = SimpleContext(name="test", value=42)
        prompt = generate_task_prompt(meta, {"query": "test"}, {"ctx": ctx})

        assert "SimpleContext" in prompt
        assert "value=42" in prompt

    def test_excludes_invisible_context(self) -> None:
        """Test that prompt excludes invisible context descriptions."""
        meta = extract_task_metadata(TaskWithMultipleContexts)
        ctx = SimpleContext(name="primary", value=1)
        session = InvisibleContext(session_id="sess_123")
        prompt = generate_task_prompt(
            meta,
            {"query": "test"},
            {"primary": ctx, "session": session},
        )

        # Should include visible context
        assert "primary" in prompt.lower() or "SimpleContext" in prompt

        # Should NOT include session_id from invisible context
        assert "sess_123" not in prompt

    def test_includes_guidance(self) -> None:
        """Test that prompt includes guidance."""
        meta = TaskWithGuidance._task_meta
        prompt = generate_task_prompt(meta, {"query": "test"}, {})

        assert "Be concise and helpful" in prompt

    def test_includes_artifact_hints(self) -> None:
        """Test that prompt includes artifact hints."""
        meta = extract_task_metadata(TaskWithArtifact)
        prompt = generate_task_prompt(meta, {"topic": "test"}, {})

        assert ".artifacts/" in prompt
        assert "doc.md" in prompt

    def test_includes_output_schema_hint(self) -> None:
        """Test that prompt includes output schema hints."""
        meta = extract_task_metadata(TaskWithMultipleOutputs)
        prompt = generate_task_prompt(meta, {"text": "test"}, {})

        assert "summary" in prompt
        assert "word_count" in prompt
        assert "sentiment" in prompt


# =============================================================================
# Test: Context Resolution
# =============================================================================


class TestContextResolution:
    """Tests for resolve_contexts()."""

    def test_resolves_by_name(self) -> None:
        """Test context resolution by field name match."""
        meta = extract_task_metadata(TaskWithContext)
        ctx = SimpleContext(name="test", value=42)

        with Scope() as scope:
            scope.bind("ctx", ctx)  # Exact name match

            resolved = resolve_contexts(meta, scope, {})

            assert "ctx" in resolved
            assert resolved["ctx"] is ctx

    def test_resolves_by_type(self) -> None:
        """Test context resolution by type match."""
        meta = extract_task_metadata(TaskWithContext)
        ctx = SimpleContext(name="test", value=42)

        with Scope() as scope:
            scope.bind("different_name", ctx)  # Different name, but right type

            resolved = resolve_contexts(meta, scope, {})

            assert "ctx" in resolved
            assert resolved["ctx"] is ctx

    def test_explicit_overrides_scope(self) -> None:
        """Test that explicit parameter overrides scope binding."""
        meta = extract_task_metadata(TaskWithContext)
        scope_ctx = SimpleContext(name="from_scope", value=1)
        explicit_ctx = SimpleContext(name="explicit", value=2)

        with Scope() as scope:
            scope.bind("ctx", scope_ctx)

            resolved = resolve_contexts(meta, scope, {"ctx": explicit_ctx})

            assert resolved["ctx"] is explicit_ctx
            assert resolved["ctx"].name == "explicit"

    def test_raises_for_missing_context(self) -> None:
        """Test that missing context raises ContextResolutionError."""
        meta = extract_task_metadata(TaskWithContext)

        with Scope() as scope:
            # No context bound

            with pytest.raises(ContextResolutionError) as exc_info:
                resolve_contexts(meta, scope, {})

            assert "ctx" in str(exc_info.value)
            assert "SimpleContext" in str(exc_info.value)

    def test_resolves_multiple_contexts(self) -> None:
        """Test resolution of multiple context fields."""
        meta = extract_task_metadata(TaskWithMultipleContexts)
        ctx1 = SimpleContext(name="primary", value=1)
        ctx2 = InvisibleContext(session_id="sess_123")

        with Scope() as scope:
            scope.bind("primary", ctx1)
            scope.bind("session", ctx2)

            resolved = resolve_contexts(meta, scope, {})

            assert len(resolved) == 2
            assert resolved["primary"] is ctx1
            assert resolved["session"] is ctx2


# =============================================================================
# Test: Output Extraction
# =============================================================================


class TestOutputExtraction:
    """Tests for extract_outputs()."""

    def test_extracts_string_output(self) -> None:
        """Test extraction of string output."""
        meta = extract_task_metadata(SimpleTask)
        result = ExecutionResult(
            success=True,
            structured_output={"result": "extracted value"},
        )

        outputs = extract_outputs(meta, result)

        assert outputs["result"] == "extracted value"

    def test_extracts_multiple_outputs(self) -> None:
        """Test extraction of multiple outputs."""
        meta = extract_task_metadata(TaskWithMultipleOutputs)
        result = ExecutionResult(
            success=True,
            structured_output={
                "summary": "A test summary",
                "word_count": 42,
                "sentiment": "positive",
            },
        )

        outputs = extract_outputs(meta, result)

        assert outputs["summary"] == "A test summary"
        assert outputs["word_count"] == 42
        assert outputs["sentiment"] == "positive"

    def test_returns_none_for_missing_output(self) -> None:
        """Test that missing outputs return None."""
        meta = extract_task_metadata(SimpleTask)
        result = ExecutionResult(
            success=True,
            structured_output={},  # No result field
        )

        outputs = extract_outputs(meta, result)

        assert outputs["result"] is None


# =============================================================================
# Test: Mock Mode
# =============================================================================


class TestMockMode:
    """Tests for MockProvider."""

    @pytest.mark.asyncio
    async def test_mock_provider_with_scope(self) -> None:
        """Test that MockProvider works with a scope."""
        with Scope(root=True) as scope:
            scope.register_provider("default", MockProvider(), default=True)
            result = await SimpleTask.run(text="test input")

            assert result.text == "test input"
            assert result.result is not None  # Mock value

    @pytest.mark.asyncio
    async def test_mock_provider_populates_outputs(self) -> None:
        """Test that MockProvider populates all output fields."""
        with Scope(root=True) as scope:
            scope.register_provider("default", MockProvider(), default=True)
            result = await TaskWithMultipleOutputs.run(text="test")

            assert result.summary is not None
            assert result.word_count is not None
            assert result.sentiment is not None

    @pytest.mark.asyncio
    async def test_mock_provider_preserves_inputs(self) -> None:
        """Test that MockProvider preserves input values."""
        with Scope(root=True) as scope:
            scope.register_provider("default", MockProvider(), default=True)
            result = await SimpleTask.run(text="preserved input")

            assert result.text == "preserved input"

    @pytest.mark.asyncio
    async def test_mock_provider_nested_scopes(self) -> None:
        """Test that nested scopes with MockProvider work."""
        with Scope(root=True) as outer:
            outer.register_provider("default", MockProvider(), default=True)
            with Scope() as inner:
                result = await SimpleTask.run(text="nested")
                assert result.result is not None

            # Still in outer scope
            result = await SimpleTask.run(text="outer")
            assert result.result is not None


# =============================================================================
# Test: Task Execution (No Scope)
# =============================================================================


class TestTaskExecutionErrors:
    """Tests for task execution error handling."""

    @pytest.mark.asyncio
    async def test_raises_without_scope_or_mock(self) -> None:
        """Test that running without scope or mock raises ScopeNotConfiguredError."""
        from shepherd_core.errors import ScopeNotConfiguredError

        with pytest.raises(ScopeNotConfiguredError) as exc_info:
            await SimpleTask.run(text="test")

        assert "No scope available" in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_raises_for_missing_context(self) -> None:
        """Test that missing context raises ContextResolutionError."""
        from shepherd_core.errors import ContextResolutionError

        with Scope(root=True) as scope:
            scope.register_provider("default", MockProvider(), default=True)
            # Context resolution happens before provider execution,
            # so missing required context raises ContextResolutionError
            with pytest.raises(ContextResolutionError) as exc_info:
                await TaskWithContext.run(query="test")

            assert "ctx" in str(exc_info.value)


# =============================================================================
# Test: Task Decorator
# =============================================================================


class TestTaskDecorator:
    """Tests for the @task decorator itself."""

    def test_adds_run_method(self) -> None:
        """Test that @task adds run() classmethod."""
        assert hasattr(SimpleTask, "run")
        assert callable(SimpleTask.run)

    def test_adds_run_sync_method(self) -> None:
        """Test that @task adds run_sync() classmethod."""
        assert hasattr(SimpleTask, "run_sync")
        assert callable(SimpleTask.run_sync)

    def test_stores_metadata(self) -> None:
        """Test that @task stores metadata on class."""
        assert hasattr(SimpleTask, "_task_meta")
        assert SimpleTask._task_meta.name == "SimpleTask"

    def test_decorator_with_parens(self) -> None:
        """Test @task() with parentheses."""

        @task()
        class TaskWithParens(BaseModel):
            x: Input(str)
            y: Output(str)

        assert hasattr(TaskWithParens, "_task_meta")
        assert hasattr(TaskWithParens, "run")

    def test_decorator_with_guidance(self) -> None:
        """Test @task(guidance=...) stores guidance."""

        @task(guidance="Custom guidance")
        class TaskCustom(BaseModel):
            x: Input(str)
            y: Output(str)

        assert TaskCustom._task_meta.guidance == "Custom guidance"


# =============================================================================
# Test: Sync Wrapper
# =============================================================================


class TestSyncWrapper:
    """Tests for run_sync() synchronous wrapper."""

    def test_run_sync_with_mock_provider(self) -> None:
        """Test that run_sync works with MockProvider."""
        with Scope(root=True) as scope:
            scope.register_provider("default", MockProvider(), default=True)
            result = SimpleTask.run_sync(text="sync test")

            assert result.text == "sync test"
            assert result.result is not None


# =============================================================================
# Test: Field Annotation Extraction
# =============================================================================


from typing import Annotated

from pydantic import Field
from shepherd_runtime.task.output import generate_output_schema


class TestFieldAnnotationExtraction:
    """Tests for extracting Pydantic Field() annotations."""

    def test_extracts_description_from_input_field(self) -> None:
        """Test that description is extracted from Field() on Input."""

        @task
        class TaskWithDescription(BaseModel):
            query: Annotated[Input(str), Field(description="Search query to process")]
            result: Output(str)

        meta = extract_task_metadata(TaskWithDescription)
        assert meta.inputs["query"].description == "Search query to process"

    def test_extracts_description_from_output_field(self) -> None:
        """Test that description is extracted from Field() on Output."""

        @task
        class TaskWithOutputDescription(BaseModel):
            text: Input(str)
            summary: Annotated[Output(str), Field(description="Summary of the text")]

        meta = extract_task_metadata(TaskWithOutputDescription)
        assert meta.outputs["summary"].description == "Summary of the text"

    def test_extracts_numeric_constraints_ge_le(self) -> None:
        """Test extraction of ge and le constraints."""

        @task
        class TaskWithConstraints(BaseModel):
            text: Input(str)
            rating: Annotated[Output(int), Field(ge=0, le=10)]

        meta = extract_task_metadata(TaskWithConstraints)
        assert meta.outputs["rating"].constraints == {"ge": 0, "le": 10}

    def test_extracts_numeric_constraints_gt_lt(self) -> None:
        """Test extraction of gt and lt constraints."""

        @task
        class TaskWithExclusiveConstraints(BaseModel):
            text: Input(str)
            score: Annotated[Output(float), Field(gt=0, lt=1)]

        meta = extract_task_metadata(TaskWithExclusiveConstraints)
        assert meta.outputs["score"].constraints == {"gt": 0, "lt": 1}

    def test_extracts_string_length_constraints(self) -> None:
        """Test extraction of min_length and max_length constraints."""

        @task
        class TaskWithLengthConstraints(BaseModel):
            name: Annotated[Input(str), Field(min_length=1, max_length=100)]
            result: Output(str)

        meta = extract_task_metadata(TaskWithLengthConstraints)
        assert meta.inputs["name"].constraints == {"min_length": 1, "max_length": 100}

    def test_constraints_extracted_but_not_in_schema(self) -> None:
        """Test that constraints are extracted but NOT included in JSON schema.

        Claude's structured outputs API does not enforce JSON Schema constraints
        like minimum/maximum. Constraints are instead:
        1. Extracted into field_info.constraints
        2. Included in the prompt text (e.g., "range 0-10")
        3. NOT included in the JSON schema

        Future work may add client-side validation with retry support.
        """

        @task
        class TaskForSchema(BaseModel):
            text: Input(str)
            rating: Annotated[Output(int), Field(ge=0, le=10, description="Rating")]

        meta = extract_task_metadata(TaskForSchema)

        # Constraints ARE extracted into metadata
        assert meta.outputs["rating"].constraints == {"ge": 0, "le": 10}

        # Constraints appear in prompt
        prompt = generate_task_prompt(meta, {"text": "test"}, {})
        assert "range 0-10" in prompt

        # Generate schema
        schema = generate_output_schema(meta)
        assert schema is not None
        props = schema["schema"]["properties"]

        # Constraints are NOT in JSON schema (API limitation)
        assert "minimum" not in props["rating"]
        assert "maximum" not in props["rating"]

        # But description IS included in schema
        assert props["rating"]["description"] == "Rating"

    def test_description_in_prompt(self) -> None:
        """Test that descriptions appear in generated prompts."""

        @task
        class TaskWithFieldDescriptions(BaseModel):
            topic: Annotated[Input(str), Field(description="Topic of the joke")]
            joke: Output(str)

        meta = extract_task_metadata(TaskWithFieldDescriptions)
        prompt = generate_task_prompt(meta, {"topic": "cats"}, {})

        assert "Topic of the joke" in prompt

    def test_constraints_in_prompt(self) -> None:
        """Test that constraints appear in generated prompts."""

        @task
        class TaskWithConstraintsInPrompt(BaseModel):
            text: Input(str)
            rating: Annotated[Output(int), Field(ge=0, le=10)]

        meta = extract_task_metadata(TaskWithConstraintsInPrompt)
        prompt = generate_task_prompt(meta, {"text": "test"}, {})

        # The prompt should mention the range
        assert "range 0-10" in prompt

    def test_combined_description_and_constraints(self) -> None:
        """Test that both description and constraints are combined."""

        @task
        class TaskCombined(BaseModel):
            text: Input(str)
            score: Annotated[
                Output(int),
                Field(description="Relevance score", ge=1, le=5),
            ]

        meta = extract_task_metadata(TaskCombined)
        prompt = generate_task_prompt(meta, {"text": "test"}, {})

        assert "Relevance score" in prompt
        assert "range 1-5" in prompt

    def test_field_without_annotations_still_works(self) -> None:
        """Test that fields without Field() still work."""

        @task
        class TaskNoAnnotations(BaseModel):
            text: Input(str)
            result: Output(str)

        meta = extract_task_metadata(TaskNoAnnotations)

        # Should have empty description and constraints
        assert meta.inputs["text"].description == ""
        assert meta.inputs["text"].constraints == {}
        assert meta.outputs["result"].description == ""
        assert meta.outputs["result"].constraints == {}

    def test_artifact_description_fallback(self) -> None:
        """Test that artifact uses marker description as fallback."""

        @task
        class TaskWithArtifactDesc(BaseModel):
            topic: Input(str)
            doc: Artifact(str, filename="output.md", description="Generated document")

        meta = extract_task_metadata(TaskWithArtifactDesc)

        # Should use the marker's description
        assert meta.artifacts["doc"].description == "Generated document"


# =============================================================================
# Tests: TaskMixin Helpful Error Messages
# =============================================================================


class TestTaskMixinHelpfulErrors:
    """Tests for helpful error messages on task instances."""

    def test_rejected_attribute_raises_helpful_error(self) -> None:
        """Accessing .rejected on task instance gives helpful error message.

        Users might accidentally try task_instance.rejected instead of
        PipelineResult.rejected. The error message should explain this.
        """
        with Scope(root=True) as scope:
            scope.register_provider("default", MockProvider(), default=True)
            result = SimpleTask(text="hello")

            # Accessing .rejected should raise helpful AttributeError
            with pytest.raises(AttributeError) as exc_info:
                _ = result.rejected

            error_message = str(exc_info.value)

            # Error should mention PipelineResult
            assert "PipelineResult" in error_message

            # Error should mention Pipeline().gate()
            assert "gate()" in error_message

            # Error should explain why
            assert "rejected" in error_message.lower()

    def test_other_missing_attributes_raise_standard_error(self) -> None:
        """Other missing attributes raise standard AttributeError."""
        with Scope(root=True) as scope:
            scope.register_provider("default", MockProvider(), default=True)
            result = SimpleTask(text="hello")

            # Accessing a truly missing attribute should raise standard error
            with pytest.raises(AttributeError) as exc_info:
                _ = result.nonexistent_attribute

            error_message = str(exc_info.value)

            # Should NOT mention PipelineResult for random attributes
            assert "PipelineResult" not in error_message

            # Should be a standard "no attribute" message
            assert "nonexistent_attribute" in error_message
