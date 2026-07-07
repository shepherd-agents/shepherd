from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

import pytest
from shepherd_core.types import ProviderBinding
from shepherd_runtime.nucleus import (
    current_workspace,
    reset_workspace_for_tests,
    workspace,
)
from shepherd_runtime.scope import current_scope
from shepherd_tests import MockProvider

if TYPE_CHECKING:
    from pathlib import Path


@pytest.fixture(autouse=True)
def reset_workspace() -> None:
    reset_workspace_for_tests()
    yield
    reset_workspace_for_tests()


def test_workspace_bare_call_installs_ambient_workspace_and_provider(tmp_path: Path) -> None:
    provider = MockProvider()
    ws = workspace(model=provider, root=tmp_path)

    assert current_workspace() is ws
    assert ws.root == tmp_path.resolve()
    assert ws.scope.get_provider() is provider


def test_workspace_foundation_surface_is_process_local_root_scope(tmp_path: Path) -> None:
    provider = MockProvider()
    ws = workspace(model=provider, root=tmp_path)

    assert ws.scope.get_provider() is provider
    assert not hasattr(ws, "tasks")
    assert not hasattr(ws, "runs")
    assert not hasattr(ws, "proposals")
    assert not hasattr(ws, "drivers")


def test_same_config_context_resets_to_previous_workspace(tmp_path: Path) -> None:
    provider = MockProvider()
    outer = workspace(model=provider, root=tmp_path)

    with workspace(model=provider, root=str(tmp_path)) as inner:
        assert current_workspace() is inner
        assert inner is not outer
        assert inner.scope is outer.scope

    assert current_workspace() is outer


def test_conflicting_workspace_replaces_when_idle(tmp_path: Path) -> None:
    # W3.4 (0.2.1 behavior change): idle reconfiguration replaces the workspace —
    # the notebook cell-re-run idiom constructs a fresh model object and must not
    # trap the session until kernel restart. Reconfiguring while a task run is
    # active still raises WorkspaceAlreadyConfigured (covered in
    # test_w0_correctness.py::TestWorkspaceReentry).
    workspace(model=MockProvider(name="first"), root=tmp_path)

    second = MockProvider(name="second")
    ws = workspace(model=second, root=tmp_path)

    assert ws.model is second
    assert current_workspace() is not None
    assert current_workspace().model is second


def test_retired_vcscore_kwarg_is_rejected_without_ambient_state(tmp_path: Path) -> None:
    with pytest.raises(TypeError, match="vcscore"):
        workspace(model=MockProvider(), root=tmp_path, vcscore=True)  # type: ignore[call-arg]

    assert current_workspace() is None
    assert current_scope() is None

    ws = workspace(model=MockProvider(), root=tmp_path)
    assert current_workspace() is ws
    assert current_scope() is ws.scope


def test_async_context_manager_resets(tmp_path: Path) -> None:
    provider = MockProvider()

    async def run() -> None:
        async with workspace(model=provider, root=tmp_path) as ws:
            assert current_workspace() is ws
        assert current_workspace() is None

    asyncio.run(run())


def test_workspace_scope_context_resets_only_inner_activation() -> None:
    with workspace(model=MockProvider()) as ws:
        assert current_scope() is ws.scope

        with ws.scope:
            assert current_scope() is ws.scope

        assert current_scope() is ws.scope

    assert current_workspace() is None
    assert current_scope() is None


def test_reset_workspace_for_tests_clears_workspace_scope() -> None:
    ws = workspace(model=MockProvider())

    assert current_workspace() is ws
    assert current_scope() is ws.scope

    reset_workspace_for_tests()

    assert current_workspace() is None
    assert current_scope() is None


def test_reset_workspace_for_tests_prevents_nested_context_exit_restoration() -> None:
    provider = MockProvider()

    with workspace(model=provider) as root:
        with workspace(model=provider) as child:
            with workspace(model=provider) as grandchild:
                assert current_workspace() is grandchild

                reset_workspace_for_tests()

                assert current_workspace() is None
                assert current_scope() is None
            assert current_workspace() is None
            assert current_scope() is None
        assert current_workspace() is None
        assert current_scope() is None
    assert current_workspace() is None
    assert current_scope() is None

    assert root is not child
    assert child is not grandchild


def test_reset_workspace_for_tests_preserves_new_workspace_after_old_context_exits() -> None:
    old_provider = MockProvider(name="old")
    new_provider = MockProvider(name="new")

    old_root = workspace(model=old_provider)
    old_child = workspace(model=old_provider)
    new_workspace = None

    try:
        old_root.__enter__()
        old_child.__enter__()
        reset_workspace_for_tests()

        new_workspace = workspace(model=new_provider)
        new_workspace.__enter__()
        assert current_workspace() is new_workspace
        assert current_scope() is new_workspace.scope

        old_child.__exit__(None, None, None)
        old_root.__exit__(None, None, None)

        assert current_workspace() is new_workspace
        assert current_scope() is new_workspace.scope
    finally:
        if new_workspace is not None:
            new_workspace.__exit__(None, None, None)
        old_child.__exit__(None, None, None)
        old_root.__exit__(None, None, None)
        reset_workspace_for_tests()

    assert current_workspace() is None
    assert current_scope() is None


def test_workspace_root_uses_private_cwd_only_binding(tmp_path: Path) -> None:
    ws = workspace(model=MockProvider(), root=tmp_path)
    binding = ws.scope.get_binding("nucleus.cwd")
    provider_binding = binding.context.configure()

    assert isinstance(provider_binding, ProviderBinding)
    assert provider_binding.cwd == str(tmp_path.resolve())
    assert provider_binding.capabilities == frozenset()
    assert provider_binding.blocked_tools == frozenset()


def test_workspace_without_root_does_not_bind_cwd_context() -> None:
    ws = workspace(model=MockProvider())

    with pytest.raises(KeyError):
        ws.scope.get_binding("nucleus.cwd")
