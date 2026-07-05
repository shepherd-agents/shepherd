from __future__ import annotations

from shepherd_runtime.nucleus import current_workspace, reset_workspace_for_tests

import shepherd
from shepherd import markers


def test_foundation_nucleus_exports_are_available() -> None:
    assert shepherd.RunRef(id="run-local").id == "run-local"
    assert callable(shepherd.workspace)
    assert callable(shepherd.task)
    assert callable(shepherd.deliver)
    assert shepherd.DeliveryFailed.__name__ == "DeliveryFailed"


def test_top_level_facade_exposes_spine_and_handle_surface() -> None:
    # WS-A (v0.1.2): `import shepherd as sp` consolidates the whole first-run API.
    # The offline syntax nucleus is exposed at the top level...
    for name in (
        "workspace",
        "Workspace",
        "task",
        "deliver",
        "handle",
        "ask",
        "tell",
        "Permissive",
        "current_binding",
        "Run",
        "RunRef",
    ):
        assert name in shepherd.__all__
    # ...and the substrate-handle surface is now consolidated here too.
    for name in (
        "GitRepo",
        "May",
        "ReadOnly",
        "ReadWrite",
        "RunOutput",
        "Changeset",
        "ShepherdWorkspace",
        "open",
        "Flow",
    ):
        assert name in shepherd.__all__
        assert getattr(shepherd, name) is not None


def test_path_scoped_grant_spelling_is_not_a_top_level_export() -> None:
    # P-030 v0.2 fence: sub-root/path-scoped grants are not part of the claim, so the GitRepoPath
    # spelling is not exposed on the top-level handle surface.
    assert not hasattr(shepherd, "GitRepoPath")
    assert "GitRepoPath" not in shepherd.__all__


def test_class_form_markers_are_not_top_level_nucleus_exports() -> None:
    removed_names = ("Input", "Output", "Context", "TaskRef", "InputMarker")

    for name in removed_names:
        assert not hasattr(shepherd, name)
        assert name not in shepherd.__all__


def test_legacy_runtime_helpers_are_not_top_level_exports() -> None:
    removed_names = ("DeliveryLimits", "Device", "TaskAdapter", "TaskFailed", "TaskMetadata", "task_fn")

    for name in removed_names:
        assert not hasattr(shepherd, name)
        assert name not in shepherd.__all__


def test_runtime_owned_delivery_limits_stays_importable_from_owner_module() -> None:
    from shepherd_runtime.nucleus import DeliveryLimits

    assert DeliveryLimits(max_turns=1).max_turns == 1


def test_artifact_top_level_export_is_function_form_dataclass() -> None:
    # `Artifact` is intentionally exported at the top level since Tranche 7 PR 21:
    # it is the frozen dataclass returned by `emit_artifact()`, not the legacy
    # class-form output marker.
    from shepherd_runtime.nucleus.artifacts import Artifact as RuntimeArtifact

    assert shepherd.Artifact is RuntimeArtifact
    assert callable(shepherd.emit_artifact)


def test_marker_namespace_only_exports_input_marker() -> None:
    assert markers.InputMarker(description="Article").description == "Article"
    assert set(markers.__all__) == {"InputMarker"}
    assert not hasattr(markers, "Input")
    assert not hasattr(markers, "Output")
    assert not hasattr(markers, "Context")
    assert not hasattr(markers, "TaskRef")


def test_reset_clears_ambient_nucleus_workspace() -> None:
    first_model = object()
    second_model = object()

    try:
        with shepherd.workspace(model=first_model):
            assert current_workspace() is not None

            reset_workspace_for_tests()

            assert current_workspace() is None
            with shepherd.workspace(model=second_model):
                assert current_workspace() is not None
    finally:
        reset_workspace_for_tests()
