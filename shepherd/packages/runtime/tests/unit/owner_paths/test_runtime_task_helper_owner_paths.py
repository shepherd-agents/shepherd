"""Tests for runtime-owned task helper owner paths and core package removal."""

from __future__ import annotations

import importlib

import pytest
from shepherd_runtime.task.artifacts import collect_artifacts as runtime_collect_artifacts
from shepherd_runtime.task.artifacts import read_artifact as runtime_read_artifact
from shepherd_runtime.task.artifacts import should_parse_json as runtime_should_parse_json
from shepherd_runtime.task.markers import Artifact as RuntimeArtifact
from shepherd_runtime.task.markers import ArtifactMarker as RuntimeArtifactMarker
from shepherd_runtime.task.markers import CompletedTask as RuntimeCompletedTask
from shepherd_runtime.task.markers import Context as RuntimeContext
from shepherd_runtime.task.markers import ContextMarker as RuntimeContextMarker
from shepherd_runtime.task.markers import Input as RuntimeInput
from shepherd_runtime.task.markers import InputMarker as RuntimeInputMarker
from shepherd_runtime.task.markers import Output as RuntimeOutput
from shepherd_runtime.task.markers import OutputMarker as RuntimeOutputMarker
from shepherd_runtime.task.markers import TaskRef as RuntimeTaskRef
from shepherd_runtime.task.pipeline import OnError as RuntimeOnError
from shepherd_runtime.task.pipeline import OnErrorPolicy as RuntimeOnErrorPolicy
from shepherd_runtime.task.pipeline import _ContinueWithPolicy as RuntimeContinueWithPolicy
from shepherd_runtime.task.pipeline import _DefaultPolicy as RuntimeDefaultPolicy
from shepherd_runtime.task.pipeline import _FatalPolicy as RuntimeFatalPolicy
from shepherd_runtime.task.pipeline import _make_stage_stub as runtime_make_stage_stub
from shepherd_runtime.task.pipeline import _SkipPolicy as RuntimeSkipPolicy


def test_runtime_task_marker_owner_path_exposes_runtime_symbols() -> None:
    assert RuntimeInput.__module__ == "shepherd_runtime.task.markers"
    assert RuntimeOutput.__module__ == "shepherd_runtime.task.markers"
    assert RuntimeContext.__module__ == "shepherd_runtime.task.markers"
    assert RuntimeArtifact.__module__ == "shepherd_runtime.task.markers"
    assert RuntimeInputMarker.__module__ == "shepherd_runtime.task.markers"
    assert RuntimeOutputMarker.__module__ == "shepherd_runtime.task.markers"
    assert RuntimeContextMarker.__module__ == "shepherd_runtime.task.markers"
    assert RuntimeArtifactMarker.__module__ == "shepherd_runtime.task.markers"
    assert RuntimeTaskRef.__module__ == "shepherd_runtime.task.markers"
    assert RuntimeCompletedTask.__module__ == "shepherd_runtime.task.markers"


def test_core_task_package_removed() -> None:
    with pytest.raises(ModuleNotFoundError):
        importlib.import_module("shepherd_core.task")


def test_runtime_task_pipeline_owner_path_exposes_runtime_symbols() -> None:
    assert RuntimeOnError.__module__ == "shepherd_runtime.task.pipeline"
    assert RuntimeOnErrorPolicy.__module__ == "shepherd_runtime.task.pipeline"
    assert RuntimeFatalPolicy.__module__ == "shepherd_runtime.task.pipeline"
    assert RuntimeSkipPolicy.__module__ == "shepherd_runtime.task.pipeline"
    assert RuntimeDefaultPolicy.__module__ == "shepherd_runtime.task.pipeline"
    assert RuntimeContinueWithPolicy.__module__ == "shepherd_runtime.task.pipeline"
    assert runtime_make_stage_stub.__module__ == "shepherd_runtime.task.pipeline"


def test_runtime_task_artifact_owner_path_exposes_runtime_symbols() -> None:
    assert runtime_collect_artifacts.__module__ == "shepherd_runtime.task.artifacts"
    assert runtime_read_artifact.__module__ == "shepherd_runtime.task.artifacts"
    assert runtime_should_parse_json.__module__ == "shepherd_runtime.task.artifacts"
