"""Tests for shepherd core functionality."""

from shepherd_contexts import (
    BashCommand,
    MCPToolCalled,
    QueryExecuted,
    SessionCreated,
    WorkspacePatchCaptured,
)
from shepherd_contexts.kvstore.effects import KeySet
from shepherd_contexts.simple_workspace.effects import SimpleWorkspaceMaterialized
from shepherd_core.context import is_reversible
from shepherd_core.effects import (
    DiffPatch,
    FileCreate,
    FilePatch,
    InputProvided,
    TaskCompleted,
    TaskStarted,
)
from shepherd_core.scope import Stream
from shepherd_export import from_json as _export_from_json
from shepherd_runtime.effects import compose_effect_registry


def from_json(source: str):
    return _export_from_json(source, registry=compose_effect_registry())


class TestEffects:
    """Test Effect types and traits."""

    def test_effect_type(self):
        """Effects should report their type name."""
        effect = FileCreate(path="foo.py", content="print(1)")
        assert effect.effect_type == "file_create"

    def test_effect_serialization(self):
        """Effects should serialize with type information."""
        effect = FileCreate(path="foo.py", content="print(1)")
        data = effect.model_dump()
        assert data["effect_type"] == "file_create"
        assert data["path"] == "foo.py"

    def test_reversible_file_create(self):
        """FileCreate should be reversible."""
        create = FileCreate(path="foo.py", content="print(1)")
        assert is_reversible(create)

        delete = create.reverse()
        assert delete.path == "foo.py"
        assert delete.had_content == "print(1)"

    def test_reversible_file_patch(self):
        """FilePatch should be reversible."""
        patch = FilePatch(path="foo.py", old_content="x", new_content="y")
        reverse = patch.reverse()

        assert reverse.path == "foo.py"
        assert reverse.old_content == "y"
        assert reverse.new_content == "x"


class TestStream:
    """Test Stream operations."""

    def test_empty_stream(self):
        """Empty stream should have no layers."""
        stream = Stream()
        assert len(stream) == 0
        assert list(stream) == []

    def test_append_effect(self):
        """Appending should return new stream with effect."""
        stream = Stream()
        effect = TaskStarted(task_name="Test")

        new_stream = stream.append(effect)

        assert len(stream) == 0  # Original unchanged
        assert len(new_stream) == 1
        assert new_stream.layers[0].effect == effect
        assert new_stream.layers[0].sequence == 0

    def test_append_multiple(self):
        """Multiple appends should maintain sequence."""
        stream = Stream()
        stream = stream.append(TaskStarted(task_name="Test"))
        stream = stream.append(InputProvided(field_name="x", value=1))
        stream = stream.append(TaskCompleted(task_name="Test"))

        assert len(stream) == 3
        assert [el.sequence for el in stream] == [0, 1, 2]

    def test_query_by_type(self):
        """Query should filter by effect type."""
        stream = Stream()
        stream = stream.append(TaskStarted(task_name="Test"))
        stream = stream.append(InputProvided(field_name="x", value=1))
        stream = stream.append(InputProvided(field_name="y", value=2))
        stream = stream.append(TaskCompleted(task_name="Test"))

        inputs = list(stream.query(InputProvided))
        assert len(inputs) == 2
        assert all(isinstance(el.effect, InputProvided) for el in inputs)

    def test_last_effect(self):
        """Last should find most recent effect of type."""
        stream = Stream()
        stream = stream.append(InputProvided(field_name="x", value=1))
        stream = stream.append(InputProvided(field_name="y", value=2))
        stream = stream.append(TaskCompleted(task_name="Test"))

        last_input = stream.last(InputProvided)
        assert last_input is not None
        assert last_input.field_name == "y"

    def test_serialization_roundtrip(self):
        """Stream should survive JSON roundtrip."""
        stream = Stream()
        stream = stream.append(TaskStarted(task_name="Test"))
        stream = stream.append(InputProvided(field_name="x", value=42))
        stream = stream.append(TaskCompleted(task_name="Test"))

        json_str = stream.to_json()
        restored = Stream.from_json(json_str)

        assert len(restored) == len(stream)
        for orig, rest in zip(stream, restored, strict=False):
            assert type(orig.effect) == type(rest.effect)
            assert orig.sequence == rest.sequence

    def test_shepherd_from_json_uses_runtime_registry(self):
        """Meta-package import helpers should inject the runtime-composed registry."""
        stream = Stream().append(SimpleWorkspaceMaterialized(changesets_applied=1, files_affected=("a.txt",)))

        restored = from_json(stream.to_json())

        assert isinstance(restored[0].effect, SimpleWorkspaceMaterialized)

    def test_shepherd_from_json_decodes_contributorized_context_effects(self):
        """Meta-package import helpers should decode installed contributor effects."""
        stream = Stream().append(KeySet(key="x", new_value="y"))

        restored = from_json(stream.to_json())

        assert isinstance(restored[0].effect, KeySet)
        assert restored[0].effect.key == "x"

    def test_shepherd_from_json_decodes_database_effect_family(self):
        """Meta-package import helpers should decode moved database effects."""
        stream = Stream().append(QueryExecuted(database="analytics", query_type="SELECT", table="users", row_count=3))

        restored = from_json(stream.to_json())

        assert isinstance(restored[0].effect, QueryExecuted)
        assert restored[0].effect.database == "analytics"

    def test_shepherd_from_json_decodes_remaining_context_effect_families(self):
        """Meta-package import helpers should decode moved context-owned effects."""
        stream = (
            Stream()
            .append(SessionCreated(session_id="sess_123"))
            .append(MCPToolCalled(server_name="filesystem", tool_name="list_directory", params={"path": "."}))
            .append(
                WorkspacePatchCaptured(
                    patch=DiffPatch(patch="diff --git a/a.txt b/a.txt\n+hello\n", files_changed=("a.txt",)),
                )
            )
            .append(BashCommand(command="ls", output="a.txt"))
        )

        restored = from_json(stream.to_json())

        assert isinstance(restored[0].effect, SessionCreated)
        assert isinstance(restored[1].effect, MCPToolCalled)
        assert isinstance(restored[2].effect, WorkspacePatchCaptured)
        assert isinstance(restored[3].effect, BashCommand)


# Note: TestMarkers, TestTaskDecorator, and TestMetadataExtraction were removed
# because they tested an older Input()/Output() marker API that has since evolved.
# The current API uses Input(T) and Output(T) as type wrappers.
# See test_context_id.py and test_capabilities.py for current usage patterns.
