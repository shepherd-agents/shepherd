"""Unit tests for pointer-linked operation projection."""

from __future__ import annotations

from dataclasses import dataclass

import pytest
import vcs_core._operation_projection as operation_projection
from vcs_core._errors import InvalidRepositoryStateError
from vcs_core._operation_projection import derive_status, project_pointer_history


@dataclass(frozen=True)
class _FakeCommit:
    id: str
    metadata: dict[str, object]
    parent_ids: tuple[str, ...] = ()
    message: str = "effect:Marker scope:task"
    commit_time: int = 1


class _FakeRepo:
    def __init__(self, commits: dict[str, _FakeCommit]) -> None:
        self._commits = commits

    def get(self, oid: object) -> _FakeCommit | None:
        return self._commits.get(str(oid))


def _oid(index: int) -> str:
    return f"{index:040x}"


def _pointer_metadata(
    *,
    operation_id: str = "operation-1",
    world_id: str = "world-1",
    parent_id: str | None = None,
    phase: str,
    seq: int,
    prev_oid: str | None,
    effect_count: int,
    result: str | None = None,
    world_disposition: str | None = None,
    nested: dict[str, object] | None = None,
) -> dict[str, object]:
    operation: dict[str, object] = {
        "id": operation_id,
        "phase": phase,
        "seq": seq,
        "prev_oid": prev_oid,
        "kind": "marker.runtime",
        "label": "Pointer Op",
        "effect_count": effect_count,
        "started_at": 100.0,
    }
    if parent_id is not None:
        operation["parent_id"] = parent_id
    if world_disposition is not None:
        operation["world_disposition"] = world_disposition
    if nested is not None:
        operation["nested"] = nested
    if result is not None:
        operation["result"] = result
        operation["closed_at"] = 200.0

    return {
        "mg": {
            "version": 1,
            "world": {
                "id": world_id,
                "ref": "refs/vcscore/scopes/task",
                "instance_id": "scope-1",
            },
            "operation": operation,
        },
    }


def _project(
    monkeypatch: pytest.MonkeyPatch,
    *commits: _FakeCommit,
) -> object:
    repo = _FakeRepo({commit.id: commit for commit in commits})
    metadata_by_id = {commit.id: commit.metadata for commit in commits}
    monkeypatch.setattr(operation_projection, "read_effect_json", lambda _repo, commit: metadata_by_id[str(commit.id)])
    monkeypatch.setattr(operation_projection.pygit2, "Commit", _FakeCommit)
    return project_pointer_history(repo, commits[0])


def test_project_pointer_history_projects_valid_chain(monkeypatch: pytest.MonkeyPatch) -> None:
    start = _FakeCommit(
        id=_oid(1),
        metadata=_pointer_metadata(phase="started", seq=0, prev_oid=None, effect_count=0),
    )
    effect = _FakeCommit(
        id=_oid(2),
        metadata=_pointer_metadata(phase="effect", seq=1, prev_oid=start.id, effect_count=1),
        parent_ids=(start.id,),
    )
    completed = _FakeCommit(
        id=_oid(3),
        metadata=_pointer_metadata(phase="completed", seq=2, prev_oid=effect.id, effect_count=1, result="ok"),
        parent_ids=(effect.id,),
    )

    projection = _project(monkeypatch, completed, effect, start)

    assert projection.operation_id == "operation-1"
    assert projection.effect_count == 1
    assert [commit.metadata["mg"]["operation"]["phase"] for commit in projection.commits] == [
        "completed",
        "effect",
        "started",
    ]


def test_project_pointer_history_rejects_prev_oid_cycle(monkeypatch: pytest.MonkeyPatch) -> None:
    effect_a = _FakeCommit(
        id=_oid(10),
        metadata=_pointer_metadata(phase="effect", seq=2, prev_oid=_oid(11), effect_count=2),
    )
    effect_b = _FakeCommit(
        id=_oid(11),
        metadata=_pointer_metadata(phase="effect", seq=1, prev_oid=effect_a.id, effect_count=2),
    )

    with pytest.raises(InvalidRepositoryStateError, match="prev_oid cycle"):
        _project(monkeypatch, effect_a, effect_b)


def test_project_pointer_history_rejects_cross_operation_prev_hop(monkeypatch: pytest.MonkeyPatch) -> None:
    start = _FakeCommit(
        id=_oid(20),
        metadata=_pointer_metadata(
            phase="started", seq=0, prev_oid=None, effect_count=0, operation_id="other-operation"
        ),
    )
    effect = _FakeCommit(
        id=_oid(21),
        metadata=_pointer_metadata(phase="effect", seq=1, prev_oid=start.id, effect_count=1),
        parent_ids=(start.id,),
    )

    with pytest.raises(InvalidRepositoryStateError, match="owned by 'other-operation'"):
        _project(monkeypatch, effect, start)


def test_project_pointer_history_rejects_pre_cutover_effect_in_chain(monkeypatch: pytest.MonkeyPatch) -> None:
    start = _FakeCommit(
        id=_oid(25),
        metadata=_pointer_metadata(phase="started", seq=0, prev_oid=None, effect_count=0),
    )
    legacy_effect = _FakeCommit(
        id=_oid(26),
        metadata={"type": "Marker", "label": "legacy"},
        parent_ids=(start.id,),
    )
    completed = _FakeCommit(
        id=_oid(27),
        metadata=_pointer_metadata(phase="completed", seq=2, prev_oid=legacy_effect.id, effect_count=1, result="ok"),
        parent_ids=(legacy_effect.id,),
    )

    with pytest.raises(InvalidRepositoryStateError, match="Unsupported pre-cutover execution history"):
        _project(monkeypatch, completed, legacy_effect, start)


def test_project_pointer_history_rejects_world_id_drift(monkeypatch: pytest.MonkeyPatch) -> None:
    start = _FakeCommit(
        id=_oid(30),
        metadata=_pointer_metadata(phase="started", seq=0, prev_oid=None, effect_count=0, world_id="world-a"),
    )
    effect = _FakeCommit(
        id=_oid(31),
        metadata=_pointer_metadata(phase="effect", seq=1, prev_oid=start.id, effect_count=1, world_id="world-b"),
        parent_ids=(start.id,),
    )

    with pytest.raises(InvalidRepositoryStateError, match=r"changes mg\.world\.id"):
        _project(monkeypatch, effect, start)


def test_project_pointer_history_rejects_parent_id_drift(monkeypatch: pytest.MonkeyPatch) -> None:
    start = _FakeCommit(
        id=_oid(40),
        metadata=_pointer_metadata(phase="started", seq=0, prev_oid=None, effect_count=0, parent_id="parent-a"),
    )
    effect = _FakeCommit(
        id=_oid(41),
        metadata=_pointer_metadata(phase="effect", seq=1, prev_oid=start.id, effect_count=1, parent_id="parent-b"),
        parent_ids=(start.id,),
    )

    with pytest.raises(InvalidRepositoryStateError, match=r"changes mg\.operation\.parent_id"):
        _project(monkeypatch, effect, start)


def test_project_pointer_history_preserves_nested_metadata(monkeypatch: pytest.MonkeyPatch) -> None:
    nested = {
        "parent_scope_ref": "refs/vcscore/ground",
        "child_scope_ref": "refs/vcscore/scopes/task",
        "ancestry_chain": ["refs/vcscore/ground"],
    }
    start = _FakeCommit(
        id=_oid(45),
        metadata=_pointer_metadata(
            phase="started",
            seq=0,
            prev_oid=None,
            effect_count=0,
            world_disposition="release",
            nested=nested,
        ),
    )

    projection = _project(monkeypatch, start)

    assert projection.world_disposition == "release"
    assert projection.nested_parent_scope_ref == "refs/vcscore/ground"
    assert projection.nested_child_scope_ref == "refs/vcscore/scopes/task"
    assert projection.nested_ancestry_chain == ("refs/vcscore/ground",)


def test_project_pointer_history_rejects_nested_metadata_drift(monkeypatch: pytest.MonkeyPatch) -> None:
    start = _FakeCommit(
        id=_oid(46),
        metadata=_pointer_metadata(
            phase="started",
            seq=0,
            prev_oid=None,
            effect_count=0,
            world_disposition="adopt",
            nested={
                "parent_scope_ref": "refs/vcscore/ground",
                "child_scope_ref": "refs/vcscore/scopes/task",
                "ancestry_chain": ["refs/vcscore/ground"],
            },
        ),
    )
    effect = _FakeCommit(
        id=_oid(47),
        metadata=_pointer_metadata(
            phase="effect",
            seq=1,
            prev_oid=start.id,
            effect_count=1,
            world_disposition="release",
            nested={
                "parent_scope_ref": "refs/vcscore/ground",
                "child_scope_ref": "refs/vcscore/scopes/task",
                "ancestry_chain": ["refs/vcscore/ground"],
            },
        ),
        parent_ids=(start.id,),
    )

    with pytest.raises(InvalidRepositoryStateError, match=r"changes mg\.operation\.world_disposition"):
        _project(monkeypatch, effect, start)


def test_project_pointer_history_rejects_non_contiguous_seq(monkeypatch: pytest.MonkeyPatch) -> None:
    start = _FakeCommit(
        id=_oid(50),
        metadata=_pointer_metadata(phase="started", seq=0, prev_oid=None, effect_count=0),
    )
    effect = _FakeCommit(
        id=_oid(51),
        metadata=_pointer_metadata(phase="effect", seq=3, prev_oid=start.id, effect_count=1),
        parent_ids=(start.id,),
    )

    with pytest.raises(InvalidRepositoryStateError, match="non-contiguous seq"):
        _project(monkeypatch, effect, start)


def test_project_pointer_history_rejects_terminal_phase_before_chain_end(monkeypatch: pytest.MonkeyPatch) -> None:
    completed = _FakeCommit(
        id=_oid(60),
        metadata=_pointer_metadata(phase="completed", seq=2, prev_oid=_oid(61), effect_count=0, result="ok"),
    )
    aborted = _FakeCommit(
        id=_oid(61),
        metadata=_pointer_metadata(phase="aborted", seq=1, prev_oid=_oid(62), effect_count=0, result="error"),
    )
    start = _FakeCommit(
        id=_oid(62),
        metadata=_pointer_metadata(phase="started", seq=0, prev_oid=None, effect_count=0),
    )

    with pytest.raises(InvalidRepositoryStateError, match="terminal phase before its anchor boundary"):
        _project(monkeypatch, completed, aborted, start)


def test_project_pointer_history_rejects_wrong_effect_count(monkeypatch: pytest.MonkeyPatch) -> None:
    start = _FakeCommit(
        id=_oid(70),
        metadata=_pointer_metadata(phase="started", seq=0, prev_oid=None, effect_count=0),
    )
    effect = _FakeCommit(
        id=_oid(71),
        metadata=_pointer_metadata(phase="effect", seq=1, prev_oid=start.id, effect_count=1),
        parent_ids=(start.id,),
    )
    completed = _FakeCommit(
        id=_oid(72),
        metadata=_pointer_metadata(phase="completed", seq=2, prev_oid=effect.id, effect_count=2, result="ok"),
        parent_ids=(effect.id,),
    )

    with pytest.raises(InvalidRepositoryStateError, match="reports effect_count=2"):
        _project(monkeypatch, completed, effect, start)


def test_derive_status_uses_phase_and_terminal_result() -> None:
    assert derive_status(phase="effect", result=None) == "open"
    assert derive_status(phase="started", result=None) == "open"
    assert derive_status(phase="completed", result="ok") == "ok"
    assert derive_status(phase="completed", result="error") == "error"
    assert derive_status(phase="completed", result="failed_exit") == "error"
    assert derive_status(phase="aborted", result="error") == "error"
