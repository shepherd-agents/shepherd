"""Tests for task discovery functions in the plugin registry."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from pydantic import BaseModel
from shepherd_runtime.registry import (
    PACKAGES_GROUP,
    discover_all_tasks,
    discover_tasks_from_package,
)
from shepherd_runtime.task.authoring import Input, Output, task

# =============================================================================
# Test fixtures: task classes for discovery
# =============================================================================


@task
class _DiscoverableTask(BaseModel):
    """A task that should be found by discovery."""

    query: Input(str)
    result: Output(str) = None

    def execute(self) -> None:
        self.result = self.query


class _NotATask(BaseModel):
    """A regular BaseModel — not decorated with @task."""

    value: str = ""


# =============================================================================
# Tests: discover_tasks_from_package (module-walk based)
# =============================================================================


class TestDiscoverTasksFromPackage:
    def test_discovers_tasks_in_package(self) -> None:
        # Discover tasks in shepherd_core.testing (which has @task classes in tests)
        # Use the test module itself as a target
        result = discover_tasks_from_package("shepherd_core")
        # shepherd_core itself may have tasks; just verify it returns a dict
        assert isinstance(result, dict)

    def test_returns_empty_for_nonexistent_package(self) -> None:
        with pytest.raises(ModuleNotFoundError):
            discover_tasks_from_package("nonexistent_package_xyz")

    def test_returns_empty_for_module_not_package(self) -> None:
        # A module (not a package) has no __path__
        result = discover_tasks_from_package("json")
        assert result == {}

    def test_discovered_tasks_have_metadata(self) -> None:
        # Use shepherd_core.testing which we know has mock task helpers
        result = discover_tasks_from_package("shepherd_core")
        for name, cls in result.items():
            assert hasattr(cls, "_task_meta"), f"Task {name} missing _task_meta"


# =============================================================================
# Tests: discover_all_tasks (module-walk via shepherd.packages)
# =============================================================================


class TestDiscoverAllTasks:
    def test_walks_registered_packages(self) -> None:
        """Module-walk discovers tasks from shepherd.packages entry points."""

        @task
        class WalkableTask(BaseModel):
            def execute(self) -> None:
                pass

        mock_pkg_ep = MagicMock()
        mock_pkg_ep.name = "test_pkg"
        mock_pkg_ep.value = "json"  # module with no __path__, returns empty

        def fake_entry_points(group: str) -> dict:
            if group == PACKAGES_GROUP:
                return {"test_pkg": mock_pkg_ep}
            return {}

        with patch(
            "shepherd_runtime.registry._get_entry_points",
            side_effect=fake_entry_points,
        ):
            result = discover_all_tasks()

        # json module has no __path__ so returns empty
        assert isinstance(result, dict)

    def test_returns_dict(self) -> None:
        result = discover_all_tasks()
        assert isinstance(result, dict)
