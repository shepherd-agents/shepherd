"""Executable coverage for the endorsed store-first quickstart."""

from __future__ import annotations

from pathlib import Path

from vcs_core import (
    DeclarativeFilesystemSubstrate,
    MarkerSubstrate,
    ScopeStack,
    Store,
    VcsCore,
    build_builtin_substrate_context,
)


def test_store_first_guide_happy_path(workspace: Path) -> None:
    store = Store(str(workspace / ".vcscore"))
    if store.is_empty:
        store.create_root_commit()

    builtins = build_builtin_substrate_context(store, workspace=workspace)
    fs = DeclarativeFilesystemSubstrate(builtins)
    marker = MarkerSubstrate(builtins)

    mg = VcsCore(str(workspace), substrates=[fs, marker], store=store)
    mg.activate()
    try:
        task = mg.fork(mg.ground, "task-fix-auth")
        marker.mark("TaskStarted", {"task": "fix-auth"})

        tool = mg.fork(task, "tool-search")
        marker.mark("ToolCallStarted", {"tool": "search"})
        fs.record_read("src/auth.py")
        marker.mark("ToolCallCompleted", {"tool": "search"})
        mg.merge(tool, task)

        tool = mg.fork(task, "tool-edit-0")
        marker.mark("ToolCallStarted", {"tool": "edit"})
        fs.record_changes([("src/auth.py", b"wrong fix")])
        marker.mark("ToolCallCompleted", {"tool": "edit", "success": False})
        archive_name = mg.discard(tool)

        tool = mg.fork(task, "tool-edit-1")
        marker.mark("ToolCallStarted", {"tool": "edit"})
        fs.record_changes([("src/auth.py", b"correct fix")])
        marker.mark("ToolCallCompleted", {"tool": "edit", "success": True})
        mg.merge(tool, task)

        tool = mg.fork(task, "tool-test")
        marker.mark("ToolCallStarted", {"tool": "test"})
        fs.record_changes([("tests/test_auth.py", b"def test_auth(): assert True")])
        marker.mark("ToolCallCompleted", {"tool": "test"})
        mg.merge(tool, task)

        tool = mg.fork(task, "tool-verify")
        marker.mark("ToolCallStarted", {"tool": "verify"})
        marker.mark("ToolCallCompleted", {"tool": "verify", "success": True})
        mg.merge(tool, task)

        marker.mark("TaskCompleted", {"task": "fix-auth"})
        mg.merge(task, mg.ground)

        assert archive_name == "tool-edit-0"
        assert any(ref.startswith("refs/vcscore/archive/tool-edit-0-") for ref in mg.store.list_archive_refs())

        status = mg.status()
        assert status.local_changes == 2
        assert status.commits_ahead > 0

        diff = mg.diff()
        changed = {entry.path: entry.status for entry in diff.files}
        assert changed == {
            "src/auth.py": "added",
            "tests/test_auth.py": "added",
        }

        marker_effects = mg.filter_effects(effect_type="Marker")
        tool_starts = [effect for effect in marker_effects if effect.metadata.get("label") == "ToolCallStarted"]
        assert {effect.metadata["metadata"]["tool"] for effect in tool_starts} == {
            "search",
            "edit",
            "test",
            "verify",
        }
        assert sum(1 for effect in tool_starts if effect.metadata["metadata"]["tool"] == "edit") == 1

        log_types = {entry.metadata["type"] for entry in mg.log(max_count=50)}
        assert "FileCreate" in log_types

        file_effects = mg.filter_effects(substrate="filesystem")
        assert {(effect.metadata.get("type"), effect.metadata.get("path")) for effect in file_effects} == {
            ("FileRead", "src/auth.py"),
            ("FileCreate", "src/auth.py"),
            ("FileCreate", "tests/test_auth.py"),
        }

        store.advance_materialized()
        materialized_status = mg.status()
        assert materialized_status.commits_ahead == 0
        assert materialized_status.local_changes == 0

        stack = ScopeStack(mg)
        assert stack.depth == 0
        stack.begin_scope("task-stack")
        stack.begin_scope("tool-stack")
        stack.commit_scope()
        stack.commit_scope()
        assert stack.depth == 0
        assert stack.current == mg.ground
    finally:
        mg.deactivate()
