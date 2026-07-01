"""Tests for runtime-owned task/step authoring entrypoints."""

from __future__ import annotations

import importlib

import pytest
from pydantic import BaseModel
from shepherd_runtime.step.api import BoundStepBuilder, InlineStep, StepBuilder, step
from shepherd_runtime.step.metadata import StepInputInfo, StepMetadata, extract_step_metadata
from shepherd_runtime.task._mixin import TaskMixin
from shepherd_runtime.task.artifacts import collect_artifacts, read_artifact, should_parse_json
from shepherd_runtime.task.authoring import task
from shepherd_runtime.task.markers import Artifact, ArtifactMarker, CompletedTask, TaskRef
from shepherd_runtime.task.metadata import FieldInfo, TaskMetadata, extract_task_metadata
from shepherd_runtime.task.pipeline import OnError, OnErrorPolicy
from shepherd_runtime.task.secure import SecurityError, secure_reconstruct_task_class
from shepherd_transform.source import (
    ReconstructionError,
    ReconstructionResult,
    extract_task_source,
    reconstruct_task_class,
    try_reconstruct_task_class,
)


def test_runtime_task_authoring_owner_path_exposes_runtime_task_decorator() -> None:
    assert task.__module__ == "shepherd_runtime.task.authoring"


def test_runtime_task_substrate_owner_path_exposes_runtime_symbols() -> None:
    assert TaskMixin.__module__ == "shepherd_runtime.task._mixin"
    source_state = importlib.import_module("shepherd_runtime.task._source_state")
    assert source_state.reconstruction_source.name == "reconstruction_source"

    @task
    class RuntimeOwnedTask(BaseModel):
        pass

    assert any(base is TaskMixin for base in RuntimeOwnedTask.__mro__)


def test_runtime_step_authoring_owner_path_exposes_runtime_symbols() -> None:
    assert step.__module__ == "shepherd_runtime.step.api"
    assert StepBuilder.__module__ == "shepherd_runtime.step.api"
    assert BoundStepBuilder.__module__ == "shepherd_runtime.step.api"
    assert InlineStep.__module__ == "shepherd_runtime.step.api"


def test_runtime_task_metadata_owner_path_exposes_runtime_symbols() -> None:
    assert FieldInfo.__module__ == "shepherd_runtime.task.metadata"
    assert TaskMetadata.__module__ == "shepherd_runtime.task.metadata"
    assert extract_task_metadata.__module__ == "shepherd_runtime.task.metadata"


def test_runtime_task_marker_owner_paths_expose_runtime_symbols() -> None:
    assert Artifact.__module__ == "shepherd_runtime.task.markers"
    assert ArtifactMarker.__module__ == "shepherd_runtime.task.markers"
    assert CompletedTask.__module__ == "shepherd_runtime.task.markers"
    assert TaskRef.__module__ == "shepherd_runtime.task.markers"


def test_runtime_task_pipeline_owner_paths_expose_runtime_symbols() -> None:
    assert OnError.__module__ == "shepherd_runtime.task.pipeline"
    assert OnErrorPolicy.__module__ == "shepherd_runtime.task.pipeline"


def test_runtime_task_artifact_owner_paths_expose_runtime_symbols() -> None:
    assert collect_artifacts.__module__ == "shepherd_runtime.task.artifacts"
    assert read_artifact.__module__ == "shepherd_runtime.task.artifacts"
    assert should_parse_json.__module__ == "shepherd_runtime.task.artifacts"


def test_runtime_step_metadata_owner_path_exposes_runtime_symbols() -> None:
    assert StepInputInfo.__module__ == "shepherd_runtime.step.metadata"
    assert StepMetadata.__module__ == "shepherd_runtime.step.metadata"
    assert extract_step_metadata.__module__ == "shepherd_runtime.step.metadata"


def test_core_step_package_removed() -> None:
    with pytest.raises(ModuleNotFoundError):
        importlib.import_module("shepherd_core.step")


def test_core_task_package_removed() -> None:
    with pytest.raises(ModuleNotFoundError):
        importlib.import_module("shepherd_core.task")


def test_runtime_task_source_module_removed() -> None:
    with pytest.raises(ModuleNotFoundError):
        importlib.import_module("shepherd_runtime.task.source")


def test_runtime_task_secure_owner_paths_expose_runtime_symbols() -> None:
    assert SecurityError.__module__ == "shepherd_runtime.task.secure"
    assert secure_reconstruct_task_class.__module__ == "shepherd_runtime.task.secure"


def test_transform_task_source_owner_paths_expose_transform_symbols() -> None:
    assert ReconstructionError.__module__ == "shepherd_transform.source"
    assert ReconstructionResult.__module__ == "shepherd_transform.source"
    assert extract_task_source.__module__ == "shepherd_transform.source"
    assert reconstruct_task_class.__module__ == "shepherd_transform.source"
    assert try_reconstruct_task_class.__module__ == "shepherd_transform.source"


def test_runtime_task_chaining_module_removed() -> None:
    with pytest.raises(ModuleNotFoundError):
        importlib.import_module("shepherd_runtime.task.chaining")


def test_runtime_task_transform_lock_module_removed() -> None:
    with pytest.raises(ModuleNotFoundError):
        importlib.import_module("shepherd_runtime.task.transform_lock")
