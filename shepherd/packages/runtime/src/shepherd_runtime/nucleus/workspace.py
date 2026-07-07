"""Ambient workspace opener for function-form task authoring."""

from __future__ import annotations

from contextvars import ContextVar, Token
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Self

from shepherd_core.context.kernel import ExecutionContextDefaults
from shepherd_core.types import ProviderBinding, ReversibilityLevel

from shepherd_runtime.scope import Scope

from .types import WorkspaceAlreadyConfigured

if TYPE_CHECKING:
    from types import TracebackType

_current_workspace: ContextVar[Workspace | None] = ContextVar("shepherd_nucleus_workspace", default=None)
_workspace_generation = 0


@dataclass(frozen=True)
class _EffectiveConfig:
    root: Path | None
    model: object

    def matches(self, *, root: Path | None, model: object) -> bool:
        """Return whether a requested workspace config is equivalent."""
        return self.root == root and self.model is model


@dataclass(frozen=True)
class _CwdOnlyBinding(ExecutionContextDefaults):
    root: Path

    @property
    def context_id(self) -> str:
        return f"nucleus.cwd:{self.root}"

    @property
    def reversibility(self) -> ReversibilityLevel:
        return ReversibilityLevel.NONE

    def configure(self, capabilities: object | None = None) -> ProviderBinding:
        del capabilities
        return ProviderBinding(
            context_id=self.context_id,
            context_type="nucleus.cwd",
            cwd=str(self.root),
            capabilities=frozenset(),
            blocked_tools=frozenset(),
        )


class Workspace:
    """Process-local syntax nucleus workspace handle."""

    def __init__(
        self,
        *,
        model: object,
        root: Path | None,
        scope: Scope,
        config: _EffectiveConfig,
        owner: Workspace | None = None,
        owns_scope: bool = False,
    ) -> None:
        self.model = model
        self.root = root
        self.scope = scope
        self._config = config
        self._owner = owner
        self._owns_scope = owns_scope
        self._workspace_token: Token[Workspace | None] | None = None
        self._workspace_generation: int | None = None
        self._scope_active = False
        self._closed = False

    @property
    def _root_owner(self) -> Workspace:
        return self if self._owner is None else self._owner._root_owner

    def _activate(self) -> None:
        if self._closed:
            raise RuntimeError("Cannot reactivate a closed Workspace")
        owner = self._root_owner
        if owner._owns_scope and not owner._scope_active:
            owner.scope.__enter__()
            owner._scope_active = True
        if self._workspace_token is None:
            self._workspace_token = _current_workspace.set(self)
            self._workspace_generation = _workspace_generation

    def _deactivate(self, exc: BaseException | None = None) -> None:
        if self._workspace_token is not None:
            if self._workspace_generation == _workspace_generation:
                _current_workspace.reset(self._workspace_token)
            self._workspace_token = None
            self._workspace_generation = None

        owner = self._root_owner
        if self is owner and owner._owns_scope and owner._scope_active:
            owner.scope.__exit__(type(exc) if exc is not None else None, exc, exc.__traceback__ if exc else None)
            owner._scope_active = False
            owner._closed = True

    def __enter__(self) -> Self:
        if self._workspace_token is None:
            self._activate()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        del exc_type, traceback
        self._deactivate(exc)

    async def __aenter__(self) -> Self:
        return self.__enter__()

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.__exit__(exc_type, exc, traceback)


def current_workspace() -> Workspace | None:
    """Return the active syntax nucleus workspace, if any."""
    return _current_workspace.get()


def workspace(*, model: object, root: str | Path | None = None) -> Workspace:
    """Open and install a process-local syntax nucleus workspace.

    The retired ``vcscore=True`` runtime-substrate spine is no longer accepted
    here. The prelaunch vcs-core run path is the workspace-control surface
    exposed through ``shepherd run ...``.
    """
    normalized_root = _normalize_root(root)
    config = _EffectiveConfig(root=normalized_root, model=model)
    existing = current_workspace()
    if existing is not None:
        if existing._config.matches(root=normalized_root, model=model):
            ws = Workspace(
                model=model,
                root=normalized_root,
                scope=existing._root_owner.scope,
                config=config,
                owner=existing._root_owner,
                owns_scope=False,
            )
            ws._activate()
            return ws
        from .delivery import active_task_run

        if active_task_run() is not None:
            raise WorkspaceAlreadyConfigured(
                "workspace(...) is already configured differently and a task run is "
                "active; finish the run before reconfiguring"
            )
        # Idle reconfiguration — the notebook/REPL cell-re-run idiom. Config
        # equality compares the model by identity, so re-running a cell that
        # constructs a fresh model object lands here even for a same-shape
        # workspace; trapping the session until kernel restart is the wrong
        # behavior when nothing is running. Tear down and replace instead.
        _teardown_current_workspace()

    scope = Scope(root=True)
    scope.register_provider("default", model, default=True)  # type: ignore[arg-type]
    if normalized_root is not None:
        scope.bind("nucleus.cwd", _CwdOnlyBinding(normalized_root))

    ws = Workspace(
        model=model,
        root=normalized_root,
        scope=scope,
        config=config,
        owns_scope=True,
    )
    ws._activate()
    return ws


def _teardown_current_workspace() -> None:
    """Close and uninstall the active nucleus workspace, if any."""
    global _workspace_generation

    _workspace_generation += 1
    ws = current_workspace()
    if ws is not None:
        owner = ws._root_owner
        if owner._owns_scope and owner._scope_active:
            owner.scope.__exit__(None, None, None)
            owner._scope_active = False
            owner._closed = True
    _current_workspace.set(None)


def reset_workspace_for_tests() -> None:
    """Clear the active nucleus workspace in tests."""
    _teardown_current_workspace()


def _normalize_root(root: str | Path | None) -> Path | None:
    if root is None:
        return None
    return Path(root).expanduser().resolve(strict=False)


__all__ = [
    "Workspace",
    "current_workspace",
    "reset_workspace_for_tests",
    "workspace",
]
