"""Shared dialect test support — fixture-resolved so combined-rootdir runs work."""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

import pytest
from vcs_core.types import FileState, normalize_git_filemode

# The root pytest config uses --import-mode=importlib, which does NOT insert a
# test's rootpath onto sys.path the way the default prepend mode does. A handful
# of tests import the shared doubles as a top-level package (`from support...`).
# Put this tests/ dir on sys.path so that resolves under both the standalone
# dialect run and the aggregate `make test` (shepherd/packages) run. Safe: this
# is the only `support` package under shepherd/packages, so no collision.
_TESTS_DIR = str(Path(__file__).resolve().parent)
if _TESTS_DIR not in sys.path:
    sys.path.insert(0, _TESTS_DIR)


class InMemoryOverlayBackend:
    """Minimal in-memory carrier so isolated forks work platform-independently."""

    def __init__(self) -> None:
        self.layers: dict[str, dict[str, FileState | None]] = {}
        self.committed: list[tuple[str, str | None]] = []
        self.discarded: list[str] = []

    def create_layer(self, scope_id: str, *, parent_scope_id: str | None) -> None:
        del parent_scope_id
        self.layers.setdefault(scope_id, {})

    def has_layer(self, scope_id: str) -> bool:
        return scope_id in self.layers

    def push_layer(self, scope_id: str | None = None) -> None:
        del scope_id

    def working_path(self, scope_id: str) -> Path:
        return Path("/virtual") / scope_id

    def diff_layer(self, scope_id: str) -> list[tuple[str, bytes | None, int]]:
        layer = self.layers.get(scope_id, {})
        return [
            (path, state.content, state.mode) if state is not None else (path, None, 0) for path, state in layer.items()
        ]

    def commit_layer(self, scope_id: str, *, into_scope_id: str | None) -> None:
        self.committed.append((scope_id, into_scope_id))

    def discard_layer(self, scope_id: str) -> None:
        self.discarded.append(scope_id)

    def read_file(self, scope_id: str, path: str) -> bytes:
        state = self.layers[scope_id][path]
        assert state is not None
        return state.content

    def read_file_state(self, scope_id: str, path: str) -> FileState:
        state = self.layers[scope_id][path]
        assert state is not None
        return state

    def write_file(self, scope_id: str, path: str, content: bytes, *, mode: int = 0o100644) -> None:
        self.layers.setdefault(scope_id, {})[path] = FileState(content, normalize_git_filemode(mode))

    def delete_file(self, scope_id: str, path: str) -> None:
        self.layers.setdefault(scope_id, {})[path] = None

    def deactivate(self) -> None:
        pass


@pytest.fixture
def overlay_backend() -> InMemoryOverlayBackend:
    return InMemoryOverlayBackend()


_NATIVE_WORKSPACE_JAIL_SKIP_REASON_UNSET = object()
_NATIVE_WORKSPACE_JAIL_SKIP_REASON: str | None | object = _NATIVE_WORKSPACE_JAIL_SKIP_REASON_UNSET


def _native_workspace_jail_skip_reason() -> str | None:
    global _NATIVE_WORKSPACE_JAIL_SKIP_REASON
    if _NATIVE_WORKSPACE_JAIL_SKIP_REASON is not _NATIVE_WORKSPACE_JAIL_SKIP_REASON_UNSET:
        assert _NATIVE_WORKSPACE_JAIL_SKIP_REASON is None or isinstance(_NATIVE_WORKSPACE_JAIL_SKIP_REASON, str)
        return _NATIVE_WORKSPACE_JAIL_SKIP_REASON

    from vcs_core._execution_capability import detect_containment_backend

    backend = detect_containment_backend()
    if backend is None:
        _NATIVE_WORKSPACE_JAIL_SKIP_REASON = "native jail backend is not available on this host"
        return _NATIVE_WORKSPACE_JAIL_SKIP_REASON
    try:
        with tempfile.TemporaryDirectory(prefix="shepherd-native-jail-probe-") as root:
            root_path = Path(root)
            for writable_roots, allow_network in (((str(root_path),), True), ((), False)):
                profile = backend.profile_for(writable_roots, allow_network=allow_network)
                backend.probe(profile, root_path, writable_roots=writable_roots)
    except Exception as exc:
        _NATIVE_WORKSPACE_JAIL_SKIP_REASON = f"native jail policy probe failed: {type(exc).__name__}: {exc}"
        return _NATIVE_WORKSPACE_JAIL_SKIP_REASON

    _NATIVE_WORKSPACE_JAIL_SKIP_REASON = None
    return None


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    del config
    native_items = [item for item in items if item.get_closest_marker("workspace_native_jail") is not None]
    if not native_items:
        return
    skip_reason = _native_workspace_jail_skip_reason()
    if skip_reason is None:
        return
    marker = pytest.mark.skip(reason=skip_reason)
    for item in native_items:
        item.add_marker(marker)
