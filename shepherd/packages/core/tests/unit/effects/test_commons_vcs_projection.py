from __future__ import annotations

import json
import math
from collections.abc import Mapping

import pytest
from commons_vcs import Edge, Object, Repo
from commons_vcs.backends import MemoryBackend
from shepherd_core.effects import FileCreate, FilePatch, ToolCallBatch, ToolCallStarted
from shepherd_core.effects.commons_vcs import (
    SHEPHERD_CAUSED_BY_ROLE,
    SHEPHERD_EFFECT_PROJECTION_VERSION,
    SHEPHERD_EFFECT_ROLE,
    SHEPHERD_EFFECT_SCHEMA,
    SHEPHERD_EVENT_SCHEMA,
    SHEPHERD_PREVIOUS_ROLE,
    ProjectedEffectStream,
    ShepherdCommonsRecorder,
    ShepherdStreamConflictError,
    ShepherdStreamRecoveryError,
    normalize_commons_value,
    project_effect_layer,
    project_effect_object,
    project_effect_stream,
    shepherd_effect_profile,
)
from shepherd_core.scope.stream import EffectLayer


def _encoded_field(value: object, field: str) -> object:
    assert isinstance(value, Mapping)
    assert value["kind"] == "object"
    for item in value["items"]:
        assert isinstance(item, tuple)
        if item[0] == field:
            return item[1]
    raise AssertionError(f"missing encoded field {field!r}")


def _encoded_effect(effect: dict[str, object]) -> dict[str, object]:
    encoded = normalize_commons_value(effect)
    assert isinstance(encoded, dict)
    return encoded


def _effect_body(effect: dict[str, object]) -> dict[str, object]:
    return {
        "projection_version": SHEPHERD_EFFECT_PROJECTION_VERSION,
        "effect_type": effect["effect_type"],
        "payload": _encoded_effect(effect),
    }


def _event_body(*, stream_id: str, sequence: int) -> dict[str, object]:
    return {
        "projection_version": SHEPHERD_EFFECT_PROJECTION_VERSION,
        "stream_id": stream_id,
        "sequence": sequence,
        "scope_depth": 0,
    }


def _append_projected(repo: Repo, projected: ProjectedEffectStream) -> list[str]:
    ids = []
    for obj in projected.objects:
        ids.append(repo.append(obj))
    return ids


def _pending_append_json(
    *,
    stream_id: str,
    expected_head: str | None,
    effect_id: str,
    event_id: str,
    sequence: int | bool,
) -> str:
    return json.dumps(
        {
            "version": 1,
            "stream_id": stream_id,
            "expected_head": expected_head,
            "effect_id": effect_id,
            "event_id": event_id,
            "sequence": sequence,
        },
        sort_keys=True,
        separators=(",", ":"),
    )


def _append_tool_started(
    repo: Repo,
    *,
    stream_id: str = "stream-a",
    sequence: int = 0,
    tool_call_id: str = "tc_1",
    previous_event_id: str | None = None,
) -> tuple[str, str]:
    effect_id = repo.append(
        Object(
            schema_ref=SHEPHERD_EFFECT_SCHEMA,
            body=_effect_body(
                {
                    "effect_type": "tool_call_started",
                    "tool_call_id": tool_call_id,
                }
            ),
        )
    )
    edges = [Edge(role=SHEPHERD_EFFECT_ROLE, target=effect_id)]
    if previous_event_id is not None:
        edges.append(Edge(role=SHEPHERD_PREVIOUS_ROLE, target=previous_event_id))
    event_id = repo.append(
        Object(
            schema_ref=SHEPHERD_EVENT_SCHEMA,
            body=_event_body(stream_id=stream_id, sequence=sequence),
            edges=tuple(edges),
        )
    )
    return effect_id, event_id


def test_project_effect_layer_splits_payload_from_occurrence_metadata() -> None:
    layer = EffectLayer(
        effect=ToolCallStarted(
            tool_call_id="tc_1",
            tool_name="Edit",
            params={"temperature": 0.25},
            timestamp=1_710_000_000.125,
        ),
        sequence=0,
        scope_id="scope-a",
        scope_depth=1,
        source_context="workspace",
    )

    projected = project_effect_layer(layer, stream_id="stream-a")

    assert projected.effect.schema_ref == SHEPHERD_EFFECT_SCHEMA
    assert projected.event.schema_ref == SHEPHERD_EVENT_SCHEMA
    assert set(projected.effect.body) == {"projection_version", "effect_type", "payload"}
    assert "scope_id" not in projected.event.body
    assert projected.event.body["source_context"] == "workspace"
    assert projected.event.edges == (Edge(role=SHEPHERD_EFFECT_ROLE, target=projected.effect.id),)
    payload = projected.effect.body["payload"]
    assert _encoded_field(payload, "timestamp") == {"kind": "float64", "repr": "1710000000.125"}
    assert _encoded_field(_encoded_field(payload, "params"), "temperature") == {"kind": "float64", "repr": "0.25"}


def test_normalize_commons_value_is_injective_for_float_like_user_objects() -> None:
    wrapped_float = normalize_commons_value(0.25)
    user_object = normalize_commons_value({"kind": "float64", "repr": "0.25"})
    legacy_wrapper_object = normalize_commons_value({"@type": "float64", "repr": "0.25"})

    assert wrapped_float == {"kind": "float64", "repr": "0.25"}
    assert user_object != wrapped_float
    assert legacy_wrapper_object != wrapped_float


def test_project_effect_stream_links_events_to_previous_and_tool_call_started_cause() -> None:
    layers = [
        EffectLayer(
            effect=ToolCallStarted(
                tool_call_id="tc_1",
                tool_name="Edit",
                params={"path": "README.md"},
                timestamp=1.0,
            ),
            sequence=0,
            scope_id="scope-a",
        ),
        EffectLayer(
            effect=FilePatch(
                path="README.md",
                old_content="before",
                new_content="after",
                caused_by="tc_1",
                timestamp=2.0,
            ),
            sequence=1,
            scope_id="scope-a",
        ),
    ]
    projected = project_effect_stream(layers, stream_id="stream-a")
    repo = Repo(profiles=[shepherd_effect_profile])

    _append_projected(repo, projected)

    first_event = projected.events[0]
    second_event = projected.events[1]
    assert second_event.edges == (
        Edge(role=SHEPHERD_EFFECT_ROLE, target=projected.effects[1].id),
        Edge(role=SHEPHERD_PREVIOUS_ROLE, target=first_event.id),
        Edge(role=SHEPHERD_CAUSED_BY_ROLE, target=first_event.id),
    )
    assert repo.cited_by(first_event.id, SHEPHERD_PREVIOUS_ROLE) == [second_event.id]
    assert repo.cited_by(first_event.id, SHEPHERD_CAUSED_BY_ROLE) == [second_event.id]


def test_project_effect_stream_has_stable_representative_object_ids() -> None:
    layers = [
        EffectLayer(
            effect=ToolCallStarted(
                tool_call_id="tc_1",
                tool_name="Edit",
                params={"path": "README.md"},
                timestamp=1.0,
            ),
            sequence=0,
            scope_id="scope-a",
        ),
        EffectLayer(
            effect=FilePatch(
                path="README.md",
                old_content="before",
                new_content="after",
                caused_by="tc_1",
                timestamp=2.0,
            ),
            sequence=1,
            scope_id="scope-a",
        ),
    ]

    projected = project_effect_stream(layers, stream_id="stream-a")

    assert [obj.id for obj in projected.objects] == [
        "sha256:3002ed5853acad390c583c528fa457ef1a236e8a0729419cbbe4cfcd39558a51",
        "sha256:170042815bce0e3d8359bc26f040fc3a32f3442ecb5b08e6896af8dd41be88d0",
        "sha256:daf0581af353b45b7d889621acb83e05f6723c89192f39474533d82653269349",
        "sha256:b5eabb6b44047a37b01a6f81bde24524c4b65d9e589571f527573ad2f156bfe7",
    ]


def test_project_effect_stream_allows_result_effect_with_null_caused_by() -> None:
    projected = project_effect_stream(
        [
            EffectLayer(effect=FileCreate(path="README.md", caused_by=None), sequence=0),
        ],
        stream_id="stream-a",
    )
    repo = Repo(profiles=[shepherd_effect_profile])

    _append_projected(repo, projected)

    assert [edge.role for edge in projected.events[0].edges] == [SHEPHERD_EFFECT_ROLE]


def test_project_effect_stream_links_tool_call_batch_as_intent_anchor() -> None:
    layers = [
        EffectLayer(effect=ToolCallBatch(batch_id="batch-1"), sequence=0),
        EffectLayer(effect=FileCreate(path="generated.txt", caused_by="batch-1"), sequence=1),
    ]
    projected = project_effect_stream(layers, stream_id="stream-a")
    repo = Repo(profiles=[shepherd_effect_profile])

    _append_projected(repo, projected)

    assert Edge(role=SHEPHERD_CAUSED_BY_ROLE, target=projected.events[0].id) in projected.events[1].edges


def test_project_effect_stream_rejects_unknown_caused_by_anchor() -> None:
    layer = EffectLayer(
        effect=FilePatch(
            path="README.md",
            old_content="before",
            new_content="after",
            caused_by="missing",
        ),
        sequence=0,
    )

    with pytest.raises(ValueError, match="unknown caused_by"):
        project_effect_stream([layer], stream_id="stream-a")


def test_project_effect_stream_rejects_out_of_order_layers() -> None:
    layers = [
        EffectLayer(effect=ToolCallStarted(tool_call_id="tc_2", tool_name="B"), sequence=1),
        EffectLayer(effect=ToolCallStarted(tool_call_id="tc_1", tool_name="A"), sequence=0),
    ]

    with pytest.raises(ValueError, match="contiguous"):
        project_effect_stream(layers, stream_id="stream-a")


def test_project_effect_stream_rejects_sequence_gaps() -> None:
    layers = [
        EffectLayer(effect=ToolCallStarted(tool_call_id="tc_1", tool_name="A"), sequence=0),
        EffectLayer(effect=ToolCallStarted(tool_call_id="tc_2", tool_name="B"), sequence=2),
    ]

    with pytest.raises(ValueError, match="expected 1, got 2"):
        project_effect_stream(layers, stream_id="stream-a")


def test_project_effect_stream_rejects_duplicate_intent_anchors() -> None:
    layers = [
        EffectLayer(effect=ToolCallBatch(batch_id="intent-1"), sequence=0),
        EffectLayer(effect=ToolCallStarted(tool_call_id="intent-1", tool_name="B"), sequence=1),
    ]

    with pytest.raises(ValueError, match="duplicate intent anchor"):
        project_effect_stream(layers, stream_id="stream-a")


def test_shepherd_commons_recorder_appends_stream_head_with_previous_edge() -> None:
    repo = Repo(profiles=[shepherd_effect_profile], backend=MemoryBackend())
    recorder = ShepherdCommonsRecorder(repo)

    first = recorder.append_layer(
        EffectLayer(effect=ToolCallStarted(tool_call_id="tc_1", tool_name="Edit"), sequence=0),
        stream_id="stream-a",
    )
    second = recorder.append_layer(
        EffectLayer(effect=FileCreate(path="README.md"), sequence=1),
        stream_id="stream-a",
    )

    assert repo.backend.get_ref(ShepherdCommonsRecorder.stream_head_ref("stream-a")) == second.event_id
    assert first.previous_head is None
    assert second.previous_head == first.event_id
    second_event = repo.get(second.event_id)
    assert second_event is not None
    assert Edge(role=SHEPHERD_PREVIOUS_ROLE, target=first.event_id) in second_event.edges
    assert repo.cited_by(first.event_id, SHEPHERD_PREVIOUS_ROLE) == [second.event_id]


def test_shepherd_commons_recorder_rejects_stale_expected_head() -> None:
    repo = Repo(profiles=[shepherd_effect_profile], backend=MemoryBackend())
    recorder = ShepherdCommonsRecorder(repo)
    first = recorder.append_layer(
        EffectLayer(effect=ToolCallStarted(tool_call_id="tc_1", tool_name="Edit"), sequence=0),
        stream_id="stream-a",
    )
    second = recorder.append_layer(
        EffectLayer(effect=FileCreate(path="README.md"), sequence=1),
        stream_id="stream-a",
        expected_head=first.event_id,
    )

    with pytest.raises(ShepherdStreamConflictError, match="expected"):
        recorder.append_layer(
            EffectLayer(effect=FileCreate(path="README-2.md"), sequence=2),
            stream_id="stream-a",
            expected_head=first.event_id,
        )

    assert repo.backend.get_ref(ShepherdCommonsRecorder.stream_head_ref("stream-a")) == second.event_id


def test_shepherd_commons_recorder_ignores_unadmitted_branch_successors() -> None:
    repo = Repo(profiles=[shepherd_effect_profile], backend=MemoryBackend())
    recorder = ShepherdCommonsRecorder(repo)
    first = recorder.append_layer(
        EffectLayer(effect=ToolCallStarted(tool_call_id="tc_1", tool_name="Edit"), sequence=0),
        stream_id="stream-a",
    )
    competing = project_effect_layer(
        EffectLayer(effect=FileCreate(path="competing.md"), sequence=1),
        stream_id="stream-a",
        previous_event_id=first.event_id,
    )
    repo.append(competing.effect)
    repo.append(competing.event)

    admitted = recorder.append_layer(
        EffectLayer(effect=FileCreate(path="recorded.md"), sequence=1),
        stream_id="stream-a",
        expected_head=first.event_id,
    )

    assert repo.backend.get_ref(ShepherdCommonsRecorder.stream_head_ref("stream-a")) == admitted.event_id
    assert sorted(repo.cited_by(first.event_id, SHEPHERD_PREVIOUS_ROLE)) == sorted(
        [competing.event.id, admitted.event_id]
    )


def test_shepherd_commons_recorder_recovers_pending_append_after_head_cas_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend = MemoryBackend()
    repo = Repo(profiles=[shepherd_effect_profile], backend=backend)
    recorder = ShepherdCommonsRecorder(repo)
    first = recorder.append_layer(
        EffectLayer(effect=ToolCallStarted(tool_call_id="tc_1", tool_name="Edit"), sequence=0),
        stream_id="stream-a",
    )
    head_ref = ShepherdCommonsRecorder.stream_head_ref("stream-a")
    original_cas = backend.compare_and_swap_ref

    def fail_head_once(name: str, expected: str | None, new: str) -> bool:
        if name == head_ref and expected == first.event_id:
            monkeypatch.setattr(backend, "compare_and_swap_ref", original_cas)
            return False
        return original_cas(name, expected, new)

    monkeypatch.setattr(backend, "compare_and_swap_ref", fail_head_once)

    with pytest.raises(ShepherdStreamConflictError, match="head changed"):
        recorder.append_layer(
            EffectLayer(effect=FileCreate(path="README.md"), sequence=1),
            stream_id="stream-a",
        )

    assert backend.get_ref(head_ref) == first.event_id
    pending_refs = list(backend.list_refs(ShepherdCommonsRecorder.pending_append_prefix("stream-a")))
    assert len(pending_refs) == 1
    recovered_head = recorder.recover_stream("stream-a")
    assert recovered_head is not None
    assert backend.get_ref(head_ref) == recovered_head
    assert list(backend.list_refs(ShepherdCommonsRecorder.pending_append_prefix("stream-a"))) == []


def test_shepherd_commons_recorder_refuses_wrong_stream_pending_recovery() -> None:
    backend = MemoryBackend()
    repo = Repo(profiles=[shepherd_effect_profile], backend=backend)
    recorder = ShepherdCommonsRecorder(repo)
    projected = project_effect_layer(
        EffectLayer(effect=FileCreate(path="wrong-stream.md"), sequence=0),
        stream_id="stream-b",
    )
    repo.append(projected.effect)
    repo.append(projected.event)
    pending = {
        "version": 1,
        "stream_id": "stream-a",
        "expected_head": None,
        "effect_id": projected.effect.id,
        "event_id": projected.event.id,
        "sequence": 0,
    }
    backend.set_ref(
        ShepherdCommonsRecorder.pending_append_ref("stream-a", projected.event.id),
        json.dumps(pending, sort_keys=True, separators=(",", ":")),
    )

    with pytest.raises(ShepherdStreamRecoveryError, match="different stream"):
        recorder.recover_stream("stream-a")

    assert backend.get_ref(ShepherdCommonsRecorder.stream_head_ref("stream-a")) is None
    assert list(backend.list_refs(ShepherdCommonsRecorder.pending_append_prefix("stream-a"))) == [
        ShepherdCommonsRecorder.pending_append_ref("stream-a", projected.event.id)
    ]


def test_shepherd_commons_recorder_refuses_pending_event_effect_mismatch() -> None:
    backend = MemoryBackend()
    repo = Repo(profiles=[shepherd_effect_profile], backend=backend)
    recorder = ShepherdCommonsRecorder(repo)
    projected = project_effect_layer(
        EffectLayer(effect=FileCreate(path="event.md"), sequence=0),
        stream_id="stream-a",
    )
    other_projected = project_effect_layer(
        EffectLayer(effect=FileCreate(path="other-effect.md"), sequence=0),
        stream_id="stream-a",
    )
    repo.append(projected.effect)
    repo.append(projected.event)
    repo.append(other_projected.effect)
    pending = {
        "version": 1,
        "stream_id": "stream-a",
        "expected_head": None,
        "effect_id": other_projected.effect.id,
        "event_id": projected.event.id,
        "sequence": 0,
    }
    backend.set_ref(
        ShepherdCommonsRecorder.pending_append_ref("stream-a", projected.event.id),
        json.dumps(pending, sort_keys=True, separators=(",", ":")),
    )

    with pytest.raises(ShepherdStreamRecoveryError, match="effect edge"):
        recorder.recover_stream("stream-a")

    assert backend.get_ref(ShepherdCommonsRecorder.stream_head_ref("stream-a")) is None
    assert list(backend.list_refs(ShepherdCommonsRecorder.pending_append_prefix("stream-a"))) == [
        ShepherdCommonsRecorder.pending_append_ref("stream-a", projected.event.id)
    ]


def test_shepherd_commons_recorder_refuses_noncanonical_pending_json() -> None:
    backend = MemoryBackend()
    repo = Repo(profiles=[shepherd_effect_profile], backend=backend)
    recorder = ShepherdCommonsRecorder(repo)
    projected = project_effect_layer(
        EffectLayer(effect=FileCreate(path="noncanonical.md"), sequence=0),
        stream_id="stream-a",
    )
    repo.append(projected.effect)
    repo.append(projected.event)
    pending_ref = ShepherdCommonsRecorder.pending_append_ref("stream-a", projected.event.id)
    backend.set_ref(
        pending_ref,
        (
            '{"stream_id":"stream-a", "version":1,'
            f'"event_id":"{projected.event.id}","sequence":0,'
            f'"expected_head":null,"effect_id":"{projected.effect.id}"}}'
        ),
    )

    with pytest.raises(ShepherdStreamRecoveryError, match="JSON is not canonical"):
        recorder.recover_stream("stream-a")

    assert backend.get_ref(ShepherdCommonsRecorder.stream_head_ref("stream-a")) is None
    assert list(backend.list_refs(ShepherdCommonsRecorder.pending_append_prefix("stream-a"))) == [pending_ref]


@pytest.mark.parametrize(
    ("mutation", "match"),
    [
        ({"unexpected": "field"}, "invalid fields"),
        ({"sequence": True}, "non-negative integer"),
        ({"version": True}, "unsupported version"),
    ],
)
def test_shepherd_commons_recorder_refuses_invalid_pending_fields(
    mutation: dict[str, object],
    match: str,
) -> None:
    backend = MemoryBackend()
    repo = Repo(profiles=[shepherd_effect_profile], backend=backend)
    recorder = ShepherdCommonsRecorder(repo)
    projected = project_effect_layer(
        EffectLayer(effect=FileCreate(path="invalid-pending.md"), sequence=0),
        stream_id="stream-a",
    )
    repo.append(projected.effect)
    repo.append(projected.event)
    pending = {
        "version": 1,
        "stream_id": "stream-a",
        "expected_head": None,
        "effect_id": projected.effect.id,
        "event_id": projected.event.id,
        "sequence": 0,
    }
    pending.update(mutation)
    pending_ref = ShepherdCommonsRecorder.pending_append_ref("stream-a", projected.event.id)
    backend.set_ref(pending_ref, json.dumps(pending, sort_keys=True, separators=(",", ":")))

    with pytest.raises(ShepherdStreamRecoveryError, match=match):
        recorder.recover_stream("stream-a")

    assert backend.get_ref(ShepherdCommonsRecorder.stream_head_ref("stream-a")) is None
    assert list(backend.list_refs(ShepherdCommonsRecorder.pending_append_prefix("stream-a"))) == [pending_ref]


def test_shepherd_commons_recorder_recovers_stale_pending_for_admitted_descendant() -> None:
    backend = MemoryBackend()
    repo = Repo(profiles=[shepherd_effect_profile], backend=backend)
    recorder = ShepherdCommonsRecorder(repo)
    first = recorder.append_layer(
        EffectLayer(effect=ToolCallStarted(tool_call_id="tc_1", tool_name="Edit"), sequence=0),
        stream_id="stream-a",
    )
    second_projected = project_effect_layer(
        EffectLayer(effect=FileCreate(path="second.md"), sequence=1),
        stream_id="stream-a",
        previous_event_id=first.event_id,
    )
    third_projected = project_effect_layer(
        EffectLayer(effect=FileCreate(path="third.md"), sequence=2),
        stream_id="stream-a",
        previous_event_id=second_projected.event.id,
    )
    repo.append(second_projected.effect)
    repo.append(second_projected.event)
    repo.append(third_projected.effect)
    repo.append(third_projected.event)
    head_ref = ShepherdCommonsRecorder.stream_head_ref("stream-a")
    assert backend.compare_and_swap_ref(head_ref, first.event_id, third_projected.event.id)
    pending = {
        "version": 1,
        "stream_id": "stream-a",
        "expected_head": first.event_id,
        "effect_id": second_projected.effect.id,
        "event_id": second_projected.event.id,
        "sequence": 1,
    }
    backend.set_ref(
        ShepherdCommonsRecorder.pending_append_ref("stream-a", second_projected.event.id),
        json.dumps(pending, sort_keys=True, separators=(",", ":")),
    )

    assert recorder.recover_stream("stream-a") == third_projected.event.id
    assert backend.get_ref(head_ref) == third_projected.event.id
    assert list(backend.list_refs(ShepherdCommonsRecorder.pending_append_prefix("stream-a"))) == []


def test_shepherd_commons_recorder_refuses_corrupt_admitted_descendant_path() -> None:
    backend = MemoryBackend()
    repo = Repo(profiles=[shepherd_effect_profile], backend=backend)
    recorder = ShepherdCommonsRecorder(repo)
    first = recorder.append_layer(
        EffectLayer(effect=ToolCallStarted(tool_call_id="tc_1", tool_name="Edit"), sequence=0),
        stream_id="stream-a",
    )
    second_projected = project_effect_layer(
        EffectLayer(effect=FileCreate(path="second.md"), sequence=1),
        stream_id="stream-a",
        previous_event_id=first.event_id,
    )
    repo.append(second_projected.effect)
    repo.append(second_projected.event)
    corrupt_head = Object(
        schema_ref=SHEPHERD_EVENT_SCHEMA,
        body=_event_body(stream_id="stream-a", sequence=2),
        edges=(Edge(SHEPHERD_PREVIOUS_ROLE, second_projected.event.id),),
    )
    corrupt_head_id = backend.write_object(corrupt_head)
    head_ref = ShepherdCommonsRecorder.stream_head_ref("stream-a")
    assert backend.compare_and_swap_ref(head_ref, first.event_id, corrupt_head_id)
    pending_ref = ShepherdCommonsRecorder.pending_append_ref("stream-a", second_projected.event.id)
    backend.set_ref(
        pending_ref,
        _pending_append_json(
            stream_id="stream-a",
            expected_head=first.event_id,
            effect_id=second_projected.effect.id,
            event_id=second_projected.event.id,
            sequence=1,
        ),
    )

    with pytest.raises(ShepherdStreamRecoveryError, match="cannot recover pending append"):
        recorder.recover_stream("stream-a")

    assert backend.get_ref(head_ref) == corrupt_head_id
    assert list(backend.list_refs(ShepherdCommonsRecorder.pending_append_prefix("stream-a"))) == [pending_ref]


def test_shepherd_commons_recorder_persists_stream_head_with_git_backend(tmp_path) -> None:
    pytest.importorskip("pygit2")
    from commons_vcs.backends.git import GitBackend

    backend = GitBackend.init(tmp_path / "repo")
    repo = Repo(profiles=[shepherd_effect_profile], backend=backend)
    recorder = ShepherdCommonsRecorder(repo)

    first = recorder.append_layer(
        EffectLayer(effect=ToolCallStarted(tool_call_id="tc_1", tool_name="Edit"), sequence=0),
        stream_id="stream-a",
    )
    second = recorder.append_layer(
        EffectLayer(effect=FileCreate(path="README.md"), sequence=1),
        stream_id="stream-a",
    )

    fresh_backend = GitBackend.open(tmp_path / "repo")
    fresh_repo = Repo(profiles=[shepherd_effect_profile], backend=fresh_backend)
    assert fresh_backend.get_ref(ShepherdCommonsRecorder.stream_head_ref("stream-a")) == second.event_id
    assert fresh_repo.get(first.event_id) is not None
    assert fresh_repo.get(second.event_id) is not None


@pytest.mark.parametrize("value", [math.nan, math.inf, {1: "bad"}, {"": "bad"}, b"bytes", {"bad": object()}])
def test_normalize_commons_value_rejects_values_outside_projection_boundary(value: object) -> None:
    with pytest.raises(TypeError):
        normalize_commons_value(value)


def test_shepherd_effect_profile_requires_strict_effect_body_fields() -> None:
    repo = Repo(profiles=[shepherd_effect_profile])
    invalid = Object(
        schema_ref=SHEPHERD_EFFECT_SCHEMA,
        body={
            **_effect_body({"effect_type": "tool_call_started", "tool_call_id": "tc_1"}),
            "stream_id": "stream-a",
        },
    )

    with pytest.raises(ValueError, match="body must contain only"):
        repo.append(invalid)


def test_shepherd_effect_profile_rejects_duplicate_encoded_payload_keys() -> None:
    repo = Repo(profiles=[shepherd_effect_profile])
    invalid = Object(
        schema_ref=SHEPHERD_EFFECT_SCHEMA,
        body={
            "projection_version": SHEPHERD_EFFECT_PROJECTION_VERSION,
            "effect_type": "tool_call_started",
            "payload": {
                "kind": "object",
                "items": [
                    ["effect_type", {"kind": "string", "value": "tool_call_started"}],
                    ["effect_type", {"kind": "string", "value": "tool_call_started"}],
                ],
            },
        },
    )

    with pytest.raises(ValueError, match="object keys must be unique and sorted"):
        repo.append(invalid)


def test_shepherd_event_profile_requires_strict_event_body_fields() -> None:
    repo = Repo(profiles=[shepherd_effect_profile])
    effect_id, _event_id = _append_tool_started(repo)
    invalid = Object(
        schema_ref=SHEPHERD_EVENT_SCHEMA,
        body={**_event_body(stream_id="stream-a", sequence=1), "scope_id": "scope-a"},
        edges=(Edge(role=SHEPHERD_EFFECT_ROLE, target=effect_id),),
    )

    with pytest.raises(ValueError, match="unsupported fields"):
        repo.append(invalid)


def test_shepherd_event_profile_rejects_unknown_edge_roles() -> None:
    repo = Repo(profiles=[shepherd_effect_profile])
    effect_id, _event_id = _append_tool_started(repo)
    invalid = Object(
        schema_ref=SHEPHERD_EVENT_SCHEMA,
        body=_event_body(stream_id="stream-a", sequence=1),
        edges=(
            Edge(role=SHEPHERD_EFFECT_ROLE, target=effect_id),
            Edge(role="shepherd.unknown", target=effect_id),
        ),
    )

    with pytest.raises(ValueError, match="unsupported"):
        repo.append(invalid)


def test_shepherd_event_profile_requires_previous_edge_after_sequence_zero() -> None:
    repo = Repo(profiles=[shepherd_effect_profile])
    effect_id, _event_id = _append_tool_started(repo)
    invalid = Object(
        schema_ref=SHEPHERD_EVENT_SCHEMA,
        body=_event_body(stream_id="stream-a", sequence=1),
        edges=(Edge(role=SHEPHERD_EFFECT_ROLE, target=effect_id),),
    )

    with pytest.raises(ValueError, match=r"requires exactly one shepherd\.previous"):
        repo.append(invalid)


def test_shepherd_event_profile_rejects_previous_edge_on_sequence_zero() -> None:
    repo = Repo(profiles=[shepherd_effect_profile])
    effect_id, target_event_id = _append_tool_started(repo)
    invalid = Object(
        schema_ref=SHEPHERD_EVENT_SCHEMA,
        body=_event_body(stream_id="stream-a", sequence=0),
        edges=(
            Edge(role=SHEPHERD_EFFECT_ROLE, target=effect_id),
            Edge(role=SHEPHERD_PREVIOUS_ROLE, target=target_event_id),
        ),
    )

    with pytest.raises(ValueError, match="sequence 0 must not"):
        repo.append(invalid)


def test_shepherd_event_profile_rejects_previous_edge_across_streams() -> None:
    repo = Repo(profiles=[shepherd_effect_profile])
    effect_id, target_event_id = _append_tool_started(repo, stream_id="stream-b")
    invalid = Object(
        schema_ref=SHEPHERD_EVENT_SCHEMA,
        body=_event_body(stream_id="stream-a", sequence=1),
        edges=(
            Edge(role=SHEPHERD_EFFECT_ROLE, target=effect_id),
            Edge(role=SHEPHERD_PREVIOUS_ROLE, target=target_event_id),
        ),
    )

    with pytest.raises(ValueError, match="same stream_id"):
        repo.append(invalid)


def test_shepherd_event_profile_rejects_previous_sequence_gap() -> None:
    repo = Repo(profiles=[shepherd_effect_profile])
    effect_id, target_event_id = _append_tool_started(repo, stream_id="stream-a", sequence=0)
    invalid = Object(
        schema_ref=SHEPHERD_EVENT_SCHEMA,
        body=_event_body(stream_id="stream-a", sequence=2),
        edges=(
            Edge(role=SHEPHERD_EFFECT_ROLE, target=effect_id),
            Edge(role=SHEPHERD_PREVIOUS_ROLE, target=target_event_id),
        ),
    )

    with pytest.raises(ValueError, match="immediately previous sequence"):
        repo.append(invalid)


def test_shepherd_event_profile_allows_branching_successors_until_recorder_admission_exists() -> None:
    repo = Repo(profiles=[shepherd_effect_profile])
    effect_id, root_event_id = _append_tool_started(repo)
    first_successor_id = repo.append(
        Object(
            schema_ref=SHEPHERD_EVENT_SCHEMA,
            body=_event_body(stream_id="stream-a", sequence=1),
            edges=(
                Edge(role=SHEPHERD_EFFECT_ROLE, target=effect_id),
                Edge(role=SHEPHERD_PREVIOUS_ROLE, target=root_event_id),
            ),
        )
    )
    second_successor_id = repo.append(
        Object(
            schema_ref=SHEPHERD_EVENT_SCHEMA,
            body=_event_body(stream_id="stream-a", sequence=1),
            edges=(
                Edge(role=SHEPHERD_EFFECT_ROLE, target=effect_id),
                Edge(role=SHEPHERD_PREVIOUS_ROLE, target=root_event_id),
            ),
        )
    )

    assert set(repo.cited_by(root_event_id, SHEPHERD_PREVIOUS_ROLE)) == {first_successor_id, second_successor_id}


def test_shepherd_event_profile_rejects_caused_by_body_without_edge() -> None:
    repo = Repo(profiles=[shepherd_effect_profile])
    _cause_effect_id, cause_event_id = _append_tool_started(repo, tool_call_id="tc_1")
    result_effect_id = repo.append(
        Object(
            schema_ref=SHEPHERD_EFFECT_SCHEMA,
            body=_effect_body({"effect_type": "file_patch", "caused_by": "tc_1"}),
        )
    )
    invalid = Object(
        schema_ref=SHEPHERD_EVENT_SCHEMA,
        body=_event_body(stream_id="stream-a", sequence=1),
        edges=(
            Edge(role=SHEPHERD_EFFECT_ROLE, target=result_effect_id),
            Edge(role=SHEPHERD_PREVIOUS_ROLE, target=cause_event_id),
        ),
    )

    with pytest.raises(ValueError, match=r"requires exactly one shepherd\.caused_by"):
        repo.append(invalid)


def test_shepherd_event_profile_rejects_caused_by_edge_without_payload_field() -> None:
    repo = Repo(profiles=[shepherd_effect_profile])
    _cause_effect_id, cause_event_id = _append_tool_started(repo, tool_call_id="tc_1")
    result_effect_id = repo.append(
        Object(
            schema_ref=SHEPHERD_EFFECT_SCHEMA,
            body=_effect_body({"effect_type": "file_patch"}),
        )
    )
    invalid = Object(
        schema_ref=SHEPHERD_EVENT_SCHEMA,
        body=_event_body(stream_id="stream-a", sequence=1),
        edges=(
            Edge(role=SHEPHERD_EFFECT_ROLE, target=result_effect_id),
            Edge(role=SHEPHERD_PREVIOUS_ROLE, target=cause_event_id),
            Edge(role=SHEPHERD_CAUSED_BY_ROLE, target=cause_event_id),
        ),
    )

    with pytest.raises(ValueError, match=r"requires effect\.payload\.caused_by"):
        repo.append(invalid)


def test_shepherd_event_profile_rejects_caused_by_target_with_wrong_anchor() -> None:
    repo = Repo(profiles=[shepherd_effect_profile])
    _cause_effect_id, cause_event_id = _append_tool_started(repo, tool_call_id="tc_2")
    result_effect_id = repo.append(
        Object(
            schema_ref=SHEPHERD_EFFECT_SCHEMA,
            body=_effect_body({"effect_type": "file_patch", "caused_by": "tc_1"}),
        )
    )
    invalid = Object(
        schema_ref=SHEPHERD_EVENT_SCHEMA,
        body=_event_body(stream_id="stream-a", sequence=1),
        edges=(
            Edge(role=SHEPHERD_EFFECT_ROLE, target=result_effect_id),
            Edge(role=SHEPHERD_PREVIOUS_ROLE, target=cause_event_id),
            Edge(role=SHEPHERD_CAUSED_BY_ROLE, target=cause_event_id),
        ),
    )

    with pytest.raises(ValueError, match="intent anchor must match"):
        repo.append(invalid)


def test_shepherd_event_profile_rejects_caused_by_target_that_is_not_intent_anchor() -> None:
    repo = Repo(profiles=[shepherd_effect_profile])
    effect_id = repo.append(
        Object(
            schema_ref=SHEPHERD_EFFECT_SCHEMA,
            body=_effect_body({"effect_type": "task_started"}),
        )
    )
    cause_event_id = repo.append(
        Object(
            schema_ref=SHEPHERD_EVENT_SCHEMA,
            body=_event_body(stream_id="stream-a", sequence=0),
            edges=(Edge(role=SHEPHERD_EFFECT_ROLE, target=effect_id),),
        )
    )
    result_effect_id = repo.append(
        Object(
            schema_ref=SHEPHERD_EFFECT_SCHEMA,
            body=_effect_body({"effect_type": "file_patch", "caused_by": "tc_1"}),
        )
    )
    invalid = Object(
        schema_ref=SHEPHERD_EVENT_SCHEMA,
        body=_event_body(stream_id="stream-a", sequence=1),
        edges=(
            Edge(role=SHEPHERD_EFFECT_ROLE, target=result_effect_id),
            Edge(role=SHEPHERD_PREVIOUS_ROLE, target=cause_event_id),
            Edge(role=SHEPHERD_CAUSED_BY_ROLE, target=cause_event_id),
        ),
    )

    with pytest.raises(ValueError, match="intent anchor must match"):
        repo.append(invalid)


def test_shepherd_event_profile_rejects_caused_by_target_at_same_or_later_sequence() -> None:
    repo = Repo(profiles=[shepherd_effect_profile])
    _root_effect_id, root_event_id = _append_tool_started(repo, tool_call_id="tc_root")
    _cause_effect_id, lateral_event_id = _append_tool_started(
        repo,
        sequence=1,
        tool_call_id="tc_1",
        previous_event_id=root_event_id,
    )
    result_effect_id = repo.append(
        Object(
            schema_ref=SHEPHERD_EFFECT_SCHEMA,
            body=_effect_body({"effect_type": "file_patch", "caused_by": "tc_1"}),
        )
    )
    invalid = Object(
        schema_ref=SHEPHERD_EVENT_SCHEMA,
        body=_event_body(stream_id="stream-a", sequence=1),
        edges=(
            Edge(role=SHEPHERD_EFFECT_ROLE, target=result_effect_id),
            Edge(role=SHEPHERD_PREVIOUS_ROLE, target=root_event_id),
            Edge(role=SHEPHERD_CAUSED_BY_ROLE, target=lateral_event_id),
        ),
    )

    with pytest.raises(ValueError, match="earlier sequence"):
        repo.append(invalid)


def test_shepherd_event_profile_rejects_duplicate_caused_by_edges() -> None:
    repo = Repo(profiles=[shepherd_effect_profile])
    _cause_effect_id, cause_event_id = _append_tool_started(repo, tool_call_id="tc_1")
    result_effect_id = repo.append(
        Object(
            schema_ref=SHEPHERD_EFFECT_SCHEMA,
            body=_effect_body({"effect_type": "file_patch", "caused_by": "tc_1"}),
        )
    )
    invalid = Object(
        schema_ref=SHEPHERD_EVENT_SCHEMA,
        body=_event_body(stream_id="stream-a", sequence=1),
        edges=(
            Edge(role=SHEPHERD_EFFECT_ROLE, target=result_effect_id),
            Edge(role=SHEPHERD_PREVIOUS_ROLE, target=cause_event_id),
            Edge(role=SHEPHERD_CAUSED_BY_ROLE, target=cause_event_id),
            Edge(role=SHEPHERD_CAUSED_BY_ROLE, target=cause_event_id),
        ),
    )

    with pytest.raises(ValueError, match=r"requires exactly one shepherd\.caused_by"):
        repo.append(invalid)


def test_project_effect_object_rejects_missing_effect_type() -> None:
    class MissingEffectType:
        def model_dump(self, *, mode: str) -> dict[str, object]:
            assert mode == "json"
            return {"path": "README.md"}

    with pytest.raises(TypeError, match="effect_type"):
        project_effect_object(MissingEffectType())
