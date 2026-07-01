from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from vcs_core import _vcscore_runtime
from vcs_core.recording import NestedParentAuthorization

if TYPE_CHECKING:
    from vcs_core.types import ScopeInfo
    from vcs_core.vcscore import VcsCore


def _open_parent_operation(mg: VcsCore, *, child_name: str = "boundary-child") -> tuple[ScopeInfo, ScopeInfo]:
    parent = mg.fork(mg.ground, "boundary-parent")
    child = mg.fork(parent, child_name)
    mg._pipeline.set_execution_context(parent)
    mg._pipeline.begin_operation(handle_id="op-parent", kind="test.parent", scope=parent)
    return parent, child


def _active_handle_refusal(*, handle_id: str, parent_ref: str, child_ref: str) -> str:
    return f"Active operation handle {handle_id!r} belongs to {parent_ref}, not {child_ref}."


@pytest.mark.parametrize("flag_value", ["0", "1"])
def test_runtime_boundary_refusal_is_byte_exact_in_both_flag_states(
    mg: VcsCore,
    monkeypatch: pytest.MonkeyPatch,
    flag_value: str,
) -> None:
    monkeypatch.setenv("VCS_CORE_NESTED_OPERATIONS", flag_value)
    parent = mg.fork(mg.ground, "boundary-parent")
    mg._pipeline.set_execution_context(parent)
    mg._pipeline.begin_operation(handle_id="op-parent", kind="test.parent", scope=parent)

    with (
        pytest.raises(RuntimeError) as exc_info,
        mg._runtime_operation_boundary(
            scope=mg.ground,
            boundary_policy="explicit",
            default_label="child",
            default_kind="test.child",
        ),
    ):
        pass

    assert str(exc_info.value) == _active_handle_refusal(
        handle_id="op-parent",
        parent_ref=parent.ref,
        child_ref=mg.ground.ref,
    )
    mg._pipeline.reset()


def test_append_or_root_never_admits_cross_scope_descendant(
    mg: VcsCore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("VCS_CORE_NESTED_OPERATIONS", "1")
    parent, child = _open_parent_operation(mg)

    with (
        pytest.raises(RuntimeError) as exc_info,
        mg._runtime_operation_boundary(
            scope=child,
            boundary_policy="append_or_root",
            default_label="child",
            default_kind="test.child",
        ),
    ):
        pass

    assert str(exc_info.value) == _active_handle_refusal(
        handle_id="op-parent",
        parent_ref=parent.ref,
        child_ref=child.ref,
    )
    mg._pipeline.reset()


def test_forged_authorization_is_rejected_at_runtime_boundary(
    mg: VcsCore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("VCS_CORE_NESTED_OPERATIONS", "1")
    parent, child = _open_parent_operation(mg)
    forged = NestedParentAuthorization(
        parent_scope_ref="refs/vcscore/scopes/not-the-parent",
        child_scope_ref=child.ref,
        ancestry_chain=("refs/vcscore/scopes/not-the-parent",),
    )

    with (
        pytest.raises(RuntimeError) as exc_info,
        mg._runtime_operation_boundary(
            scope=child,
            boundary_policy="explicit",
            default_label="child",
            default_kind="test.child",
            nested_parent=forged,
        ),
    ):
        pass

    assert str(exc_info.value) == _active_handle_refusal(
        handle_id="op-parent",
        parent_ref=parent.ref,
        child_ref=child.ref,
    )
    mg._pipeline.reset()


@pytest.mark.parametrize("boundary_policy", ["explicit", "forced_child"])
def test_authorized_direct_runtime_boundary_opens_and_restores_parent(
    mg: VcsCore,
    monkeypatch: pytest.MonkeyPatch,
    boundary_policy: str,
) -> None:
    monkeypatch.setenv("VCS_CORE_NESTED_OPERATIONS", "1")
    parent, child = _open_parent_operation(mg)
    parent_operation = mg._pipeline.current_operation()
    assert parent_operation is not None

    with mg._runtime_operation_boundary(
        scope=child,
        boundary_policy=boundary_policy,
        default_label="child",
        default_kind="test.child",
        operation_id=f"child-{boundary_policy}",
    ) as child_operation:
        assert child_operation is not None
        assert child_operation.nested_parent_scope_ref == parent.ref
        assert child_operation.nested_child_scope_ref == child.ref
        assert mg._pipeline.current_operation() is not None
        assert mg._pipeline.current_operation().ref == child_operation.ref

    assert mg._pipeline.current_operation() is not None
    assert mg._pipeline.current_operation().ref == parent_operation.ref
    mg._pipeline.reset()


def test_runtime_boundary_uses_one_walk_object_for_begin_operation(
    mg: VcsCore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("VCS_CORE_NESTED_OPERATIONS", "1")
    _parent, child = _open_parent_operation(mg)
    original_walk = _vcscore_runtime._nested_parent_authorization
    original_begin = mg._pipeline.begin_operation
    walked: list[NestedParentAuthorization | None] = []
    observed_by_begin: list[NestedParentAuthorization | None] = []

    def walk_spy(owner: VcsCore, scope: ScopeInfo) -> NestedParentAuthorization | None:
        authorization = original_walk(owner, scope)
        walked.append(authorization)
        return authorization

    def begin_spy(*args: object, **kwargs: object):
        nested_parent = kwargs.get("nested_parent")
        assert nested_parent is None or isinstance(nested_parent, NestedParentAuthorization)
        observed_by_begin.append(nested_parent)
        return original_begin(*args, **kwargs)

    monkeypatch.setattr(_vcscore_runtime, "_nested_parent_authorization", walk_spy)
    monkeypatch.setattr(mg._pipeline, "begin_operation", begin_spy)

    with mg._runtime_operation_boundary(
        scope=child,
        boundary_policy="forced_child",
        default_label="child",
        default_kind="test.child",
    ):
        pass

    assert len(walked) == 1
    assert walked[0] is not None
    assert observed_by_begin == [walked[0]]
    mg._pipeline.reset()


def test_complete_error_failure_policy_still_requires_root_activity(mg: VcsCore) -> None:
    parent, _child = _open_parent_operation(mg)

    with (
        pytest.raises(RuntimeError) as exc_info,
        mg._runtime_operation_boundary(
            scope=parent,
            boundary_policy="explicit",
            default_label="child",
            default_kind="test.child",
            failure_policy="complete_error",
        ),
    ):
        pass

    assert str(exc_info.value) == "failure_policy='complete_error' requires a root runtime activity."
    mg._pipeline.reset()
