"""Task discovery helpers for runtime-owned authoring surfaces."""

from __future__ import annotations

import importlib
import logging
import pkgutil
from typing import Any

from shepherd_core.config import is_strict_mode
from shepherd_core.errors import PluginLoadError

logger = logging.getLogger(__name__)

TASKS_GROUP = "shepherd.tasks"
PACKAGES_GROUP = "shepherd.packages"


def discover_tasks_from_package(package_name: str) -> dict[str, type]:
    """Discover all ``@task`` classes in a package by walking its modules."""
    package = importlib.import_module(package_name)
    if not hasattr(package, "__path__"):
        return {}

    tasks: dict[str, type] = {}

    for _importer, modname, _ispkg in pkgutil.walk_packages(package.__path__, prefix=package.__name__ + "."):
        try:
            module = importlib.import_module(modname)
        except Exception:  # noqa: BLE001
            logger.debug("Skipping module %s (import failed)", modname)
            continue

        for attr_name in dir(module):
            obj = getattr(module, attr_name, None)
            if isinstance(obj, type) and hasattr(obj, "_task_meta") and obj.__module__ == modname:
                tasks[obj._task_meta.name] = obj

    return tasks


def discover_all_tasks(*, get_entry_points: Any) -> dict[str, type]:
    """Discover tasks by walking all registered packages."""
    tasks: dict[str, type] = {}
    for name, ep in get_entry_points(PACKAGES_GROUP).items():
        try:
            package_name = ep.value.split(":")[0]
            tasks.update(discover_tasks_from_package(package_name))
        except Exception as exc:
            if is_strict_mode():
                raise PluginLoadError(name, PACKAGES_GROUP, exc) from exc
            logger.warning("Failed to walk package '%s'", name, exc_info=logger.isEnabledFor(logging.DEBUG))

    return tasks


__all__ = [
    "PACKAGES_GROUP",
    "TASKS_GROUP",
    "discover_all_tasks",
    "discover_tasks_from_package",
]
