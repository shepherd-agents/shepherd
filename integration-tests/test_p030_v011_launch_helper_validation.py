"""Fast validation coverage for the visual-artifact notebook helpers."""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from shepherd_dialect.workspace_control import RUN_ARTIFACT_INPUT_SCHEMA, RunArtifactInputRef

REPO = Path(__file__).resolve().parents[1]
VISUAL_ARTIFACT_EXAMPLE = REPO / "examples" / "notebooks" / "visual_artifact"


@pytest.fixture(scope="module")
def launch_helpers():
    """Import the notebook helper using the same path shape as the notebooks."""
    if str(VISUAL_ARTIFACT_EXAMPLE) not in sys.path:
        sys.path.insert(0, str(VISUAL_ARTIFACT_EXAMPLE))
    from shepherd_usecases.visual_artifact import launch

    return launch


def test_launch_helper_rejects_reserved_artifact_ref_names_without_workspace(launch_helpers) -> None:
    """Artifact citations cannot overwrite provider control fields."""
    ref = _artifact_ref()

    for reserved in ("output_path", "output_text", "output_content", "artifact_path", "artifact_text", "runtime"):
        with pytest.raises(ValueError, match="artifact ref name is reserved"):
            launch_helpers._validated_artifact_refs({reserved: ref})

    with pytest.raises(ValueError, match="artifact ref name is reserved"):
        launch_helpers._validated_artifact_refs({"skeleton_internal": ref})

    with pytest.raises(ValueError, match="artifact refs must be a non-empty mapping"):
        launch_helpers._validated_artifact_refs({})


def test_launch_helper_accepts_canonical_artifact_ref_json_without_workspace(launch_helpers) -> None:
    """JSON-shaped artifact refs are normalized before helper forwarding."""
    validated = launch_helpers._validated_artifact_refs({"source": _artifact_ref(label="source").to_json()})

    assert set(validated) == {"source"}
    ref = validated["source"]
    assert isinstance(ref, RunArtifactInputRef)
    assert ref.run_ref == "run-source"
    assert ref.output_id == "output-source"
    assert ref.path == "source.json"
    assert ref.label == "source"


@pytest.mark.parametrize(
    ("value", "message"),
    [
        ("not-a-ref", "must be an artifact reference"),
        ({"not": "an artifact ref"}, "must be an artifact reference"),
        ({"kind": RUN_ARTIFACT_INPUT_SCHEMA, "run_ref": ""}, "is malformed"),
    ],
)
def test_launch_helper_rejects_non_artifact_ref_values_without_workspace(
    launch_helpers,
    value: object,
    message: str,
) -> None:
    """Bad artifact refs are rejected before any workspace launch is needed."""
    with pytest.raises(ValueError, match=message):
        launch_helpers._validated_artifact_refs({"source": value})


def test_launch_helper_forwards_validated_artifact_refs_to_static_flow_without_workspace(launch_helpers) -> None:
    """The public helper preserves validated refs and static provider defaults."""
    flow = _FakeFlow()
    repo = object()
    workspace = SimpleNamespace(flow=flow, repo=repo)
    after = (object(),)
    metadata = {"purpose": "fast-validation"}
    ref_json = _artifact_ref(label="source").to_json()

    result = launch_helpers.run_with_artifact_refs(
        workspace,
        name="consumer",
        refs={"source": ref_json},
        output_path="consumer.json",
        output_content={"used": True},
        after=after,
        metadata=metadata,
    )

    assert result == "fake-run"
    assert len(flow.calls) == 1
    call = flow.calls[0]
    assert call["task_ref"] == launch_helpers.TASK_REF
    assert call["repo"] is repo
    assert call["name"] == "consumer"
    assert call["after"] is after
    assert call["runtime"] == {"provider": "static"}
    assert call["placement"] == "advisory"
    assert call["metadata"] is metadata
    args = call["args"]
    assert args["output_path"] == "consumer.json"
    assert args["output_content"] == {"used": True}
    assert isinstance(args["source"], RunArtifactInputRef)
    assert args["source"].label == "source"


def test_launch_helper_registers_live_tasks_lazily_without_workspace(launch_helpers) -> None:
    """Static setup avoids live task registration until a Claude helper is used."""
    flow = _FakeFlow()
    tasks = _FakeTasks()
    repo = object()
    workspace = launch_helpers.LaunchWorkspace(
        control=SimpleNamespace(tasks=tasks),
        flow=flow,
        repo=repo,
        root=Path("unused"),
    )

    artifact = launch_helpers.run_claude_artifact(
        workspace,
        name="live-artifact",
        prompt="make a tile",
        variant="contour-map",
        instruction="write index.html",
        model="sonnet",
    )
    reviewer = launch_helpers.run_claude_review(
        workspace,
        name="live-review",
        prompt="judge the tile",
        refs={"source": _artifact_ref(label="source").to_json()},
        after=(artifact,),
        model="sonnet",
    )

    assert artifact == "fake-run"
    assert reviewer == "fake-run"
    assert tasks.registrations == [
        launch_helpers.LIVE_ARTIFACT_TASK_REF,
        launch_helpers.LIVE_REVIEW_TASK_REF,
    ]
    assert workspace._live_tasks_registered is True
    assert [call["task_ref"] for call in flow.calls] == [
        launch_helpers.LIVE_ARTIFACT_TASK_REF,
        launch_helpers.LIVE_REVIEW_TASK_REF,
    ]
    assert flow.calls[0]["runtime"] == {"provider": "claude", "model": "sonnet"}
    assert flow.calls[0]["placement"] == "auto"
    assert flow.calls[1]["runtime"] == {"provider": "claude", "model": "sonnet"}
    assert flow.calls[1]["placement"] == "auto"
    assert isinstance(flow.calls[1]["args"]["source"], RunArtifactInputRef)


class _FakeFlow:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def fork(self, task_ref: str, **kwargs: Any) -> str:
        self.calls.append({"task_ref": task_ref, **kwargs})
        return "fake-run"


class _FakeTasks:
    def __init__(self) -> None:
        self.registrations: list[str] = []

    def register(self, source: str, *, may_default: str) -> None:
        assert may_default == "ReadWrite"
        self.registrations.append(source)


def _artifact_ref(*, label: str | None = None) -> RunArtifactInputRef:
    return RunArtifactInputRef(
        run_ref="run-source",
        output_id="output-source",
        path="source.json",
        label=label,
        content_digest="sha256:" + ("1" * 64),
    )
