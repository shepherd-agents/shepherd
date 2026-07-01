"""Tests for production v2 world-storage installation helpers."""

from __future__ import annotations

import json

import pytest
from vcs_core._errors import InvalidRepositoryStateError
from vcs_core._world_storage_installation import (
    DEFAULT_SHEPHERD_TASK_ARTIFACTS_STORE_ID,
    DEFAULT_WORKSPACE_STORE_ID,
    DEFAULT_WORLD_STORE_ID,
    default_world_storage_exists,
    default_world_storage_root,
    open_existing_default_world_storage,
    open_or_init_default_world_storage,
)
from vcs_core._world_storage_manager import SubstrateStoreSpec, WorldStorageManager
from vcs_core._world_types import SubstrateStoreIdentity
from vcs_core.store import Store
from vcs_core.vcscore import VcsCore


def test_default_world_storage_opens_stable_workspace_store(tmp_path) -> None:
    repo_path = tmp_path / ".vcscore"
    # The default install expects the scalar vcs-core store to exist at
    # repo_path; substrate alternates point at its ODB for tree-backed
    # workspace revisions.
    Store(str(repo_path))
    manager = open_or_init_default_world_storage(repo_path)
    reopened = open_or_init_default_world_storage(repo_path)
    existing = open_existing_default_world_storage(repo_path)

    assert manager.root == default_world_storage_root(repo_path)
    assert manager.world_store.world_store_id == DEFAULT_WORLD_STORE_ID
    assert DEFAULT_WORKSPACE_STORE_ID in manager.stores
    assert DEFAULT_SHEPHERD_TASK_ARTIFACTS_STORE_ID in manager.stores
    assert reopened.locator_hints()[DEFAULT_WORKSPACE_STORE_ID] == "substrates/workspace.git"
    assert (
        reopened.locator_hints()[DEFAULT_SHEPHERD_TASK_ARTIFACTS_STORE_ID]
        == "substrates/shepherd-task-artifacts.git"
    )
    assert existing.locator_hints()[DEFAULT_WORKSPACE_STORE_ID] == "substrates/workspace.git"

    config = json.loads((manager.root / "world-stores.json").read_text())
    workspace = config["stores"][DEFAULT_WORKSPACE_STORE_ID]["identity"]
    assert workspace["store_id"] == DEFAULT_WORKSPACE_STORE_ID
    assert workspace["kind"] == "filesystem"
    assert workspace["resource_id"] == "fs:repo-main"
    artifacts = config["stores"][DEFAULT_SHEPHERD_TASK_ARTIFACTS_STORE_ID]["identity"]
    assert artifacts["store_id"] == DEFAULT_SHEPHERD_TASK_ARTIFACTS_STORE_ID
    assert artifacts["kind"] == "shepherd.task-artifacts"
    assert artifacts["resource_id"] == "shepherd-task-artifacts:main"


def test_default_world_storage_exists_does_not_initialize(tmp_path) -> None:
    repo_path = tmp_path / ".vcscore"

    assert not default_world_storage_exists(repo_path)
    assert not default_world_storage_root(repo_path).exists()


def test_default_world_storage_rejects_mismatched_installation(tmp_path) -> None:
    repo_path = tmp_path / ".vcscore"
    Store(str(repo_path))
    root = default_world_storage_root(repo_path)
    open_or_init_default_world_storage(repo_path)

    with pytest.raises(InvalidRepositoryStateError, match="world_store_id mismatch"):
        WorldStorageManager.open_or_init(
            root,
            world_store_id="store_world_other",
            stores=(
                SubstrateStoreSpec(
                    identity=SubstrateStoreIdentity(
                        store_id=DEFAULT_WORKSPACE_STORE_ID,
                        kind="filesystem",
                        resource_id="fs:repo-main",
                    ),
                    locator="substrates/workspace.git",
                ),
            ),
        )


def test_vcscore_world_storage_is_lazy_and_cached(tmp_path) -> None:
    repo_path = tmp_path / ".vcscore"
    store = Store(str(repo_path))
    store.create_root_commit()
    mg = VcsCore(str(tmp_path), store=store)

    assert not default_world_storage_root(repo_path).exists()

    first = mg._world_storage()
    second = mg._world_storage()

    assert first is second
    assert default_world_storage_root(repo_path).exists()
    assert first.world_store.world_store_id == DEFAULT_WORLD_STORE_ID
