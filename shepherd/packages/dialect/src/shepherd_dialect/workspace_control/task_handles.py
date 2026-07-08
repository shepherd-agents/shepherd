"""Thin public task wrapper for the workspace-control floor."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal

from shepherd_dialect.workspace_control.identities import TaskRef

if TYPE_CHECKING:
    from collections.abc import Mapping

    from shepherd_runtime.nucleus import GitRepo

    from shepherd_dialect.workspace_control.run_handles import WorkspaceRun
    from shepherd_dialect.workspace_control.schemas import TaskDefinitionVersion
    from shepherd_dialect.workspace_control.workspace import ShepherdWorkspace


class _UnsetType:
    """Sentinel for an option not passed to :meth:`WorkspaceTask.run`.

    An unset option is not forwarded, so ``ShepherdWorkspace.run`` applies its own default
    (and its shadow-name guard reads the option as unpassed).
    """

    def __repr__(self) -> str:
        return "<unset>"


_UNSET: Any = _UnsetType()


@dataclass(frozen=True, eq=False)
class WorkspaceTask:
    """Task identity plus workspace context for the handle-in run facade.

    This wrapper is a product noun, not a new runtime. ``run(...)`` delegates
    to ``ShepherdWorkspace.run(...)`` so selected-GitRepo validation, launch,
    output publication, and retained custody stay on the existing spine.
    """

    _workspace: ShepherdWorkspace = field(repr=False, compare=False)
    _task_ref: str

    @property
    def task_ref(self) -> str:
        return self._task_ref

    @property
    def ref(self) -> TaskRef:
        """Return this task's typed public identity value."""
        return TaskRef(self._task_ref)

    @property
    def definition(self) -> TaskDefinitionVersion | None:
        """Return the current task definition, if this ref resolves."""
        return self._workspace.tasks.get(self.ref)

    def run(
        self,
        *,
        repo: GitRepo | None = None,
        bindings: Mapping[str, GitRepo] | None = None,
        args: Mapping[str, Any] | None = None,
        may: str | None = _UNSET,
        placement: Literal["auto", "advisory", "jail"] = _UNSET,
        runtime: Mapping[str, object] | None = _UNSET,
        **task_args: Any,
    ) -> WorkspaceRun:
        """Run this task through the workspace-control handle-in facade.

        Exactly one of ``repo`` (single selected binding) / ``bindings`` (named
        multi-binding, Lane C) is given — parity with :meth:`ShepherdWorkspace.run`,
        including keyword task arguments (``task.run(repo=h, topic="x")``).
        """
        # Forward only options the caller set, so ShepherdWorkspace.run applies its own
        # defaults and its shadow-name guard reads unpassed options as unpassed.
        options: dict[str, Any] = {}
        if not isinstance(may, _UnsetType):
            options["may"] = may
        if not isinstance(placement, _UnsetType):
            options["placement"] = placement
        if not isinstance(runtime, _UnsetType):
            options["runtime"] = runtime
        return self._workspace.run(
            self.ref, repo=repo, bindings=bindings, args=args, **options, **task_args
        )

    def to_json(self) -> dict[str, object]:
        """Return a compact JSON-shaped projection."""
        definition = self.definition
        return {
            "task_ref": self.task_ref,
            "definition": None if definition is None else definition.to_json(),
        }
