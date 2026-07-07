"""vcs-core test-support seam — supported for tests, NOT the product API.

Cross-module test code needs a handful of vcs-core internals as fixtures,
builders, and records (a session-info record, the world-storage manager and its
store spec, a hook manager, the operation-journal ref helper, the world-operation
builders). Before this seam, ~113 test-import instances reached directly into
`vcs_core._*` private modules, so any internal move broke the suite at collection
time. This module centralises that reach into one sanctioned, guarded place: the
private imports live here, and tests import from `vcs_core.testing`.

Discipline (guarded by test_vcs_core_testing_boundary.py):
- this module imports no `shepherd`/`shepherd2` code;
- no production `vcs_core` module imports from `vcs_core.testing` (one-way).

Not here (deliberate): `SessionDaemon`. Its one cross-module use is a
`monkeypatch.setattr` on the real `_session` module attribute, which a re-export
cannot serve (patching must target the module the daemon is read from). A daemon
harness/fake seam is deferred until a genuine consumer exists (260704-1410-plan
D-B). This is the general test seam; the SPI conformance kit is `vcs_core.spi.testing`.
"""

from __future__ import annotations

from vcs_core._dirty_flag import write_dirty_flag
from vcs_core._hooks import HookManager
from vcs_core._ipc import SessionInfo
from vcs_core._substrate_tree_read import read_substrate_workspace_file
from vcs_core._world_operation_builder import CandidateSelection, OperationFinalBuilder
from vcs_core._world_refs import operation_journal_ref
from vcs_core._world_storage_manager import (
    SubstrateStoreSpec,
    WorldStorageManager,
)
from vcs_core._world_storage_records import DEFAULT_GROUND_REF

__all__ = [
    "DEFAULT_GROUND_REF",
    "CandidateSelection",
    "HookManager",
    "OperationFinalBuilder",
    "SessionInfo",
    "SubstrateStoreSpec",
    "WorldStorageManager",
    "operation_journal_ref",
    "read_world_workspace_file",
    "write_dirty_flag",
]


def read_world_workspace_file(
    manager: WorldStorageManager, world_oid: str, path: str, *, binding: str = "workspace"
) -> bytes | None:
    """Read one file's bytes from a published world's ``binding`` head (tests only).

    Settlement verbs publish worlds without materializing the working directory, so tests that
    assert on post-settlement content read from the world head rather than disk. Returns the file
    bytes, or ``None`` when the binding/head/path does not resolve. Centralizes the
    ``read_world`` → head → store → ``read_substrate_workspace_file`` chain that settlement tests
    across packages would otherwise each reach into ``vcs_core._*`` to perform.
    """
    world = manager.read_world(world_oid)
    head = world.snapshot.head_for(binding)
    substrate = manager.store(head.store_id)
    result = read_substrate_workspace_file(substrate.repo, head.head, path)
    return None if result is None else result[0]
