"""Portable copy carrier backend for FilesystemSubstrate (all platforms).

The universal member of the ``CarrierBackend`` family (the reversibility axis,
_substrate_runtime.py) and the last-resort floor when no faster carrier is
available: no copy-on-write, no union mount, no kernel/FUSE requirement, no
elevated privileges — each scope layer is a plain recursive copy of its base, so
it behaves identically on macOS, Linux, and Linux-on-WSL.

Reversibility holds trivially: a copy is an independent tree, so discard is an
``rmtree`` and merge is a diff-and-apply — the same contract the overlay and
clonefile carriers provide. The only thing given up is block-sharing, so capture
cost scales with workspace size (``diff_layer`` reads every file). That is fine
at the quickstart / small-workspace scale this floor targets; native carriers
(kernel/FUSE overlay on Linux, APFS clonefile on macOS) are preferred whenever
the platform offers them.

``ClonefileCarrierBackend`` (``_clonefile_carrier.py``) is a macOS CoW
specialization of this backend that overrides only ``_clone_tree`` (APFS
``cp -c`` block-sharing clone) and ``_ensure_supported`` (the darwin guard).
Internal runtime surface — not part of the frozen consumer SPI.
"""

from __future__ import annotations

import os
import shutil
import stat
from pathlib import Path, PurePosixPath

from vcs_core._errors import UnsupportedOverlayEntryError
from vcs_core._overlay_entries import unsupported_overlay_entry_kind
from vcs_core.types import FileState, normalize_git_filemode, posix_to_git_mode


class CopyCarrierBackend:
    """Portable carrier: each scope is a plain recursive copy of its base.

    A child scope copies its parent's working tree; the ground scope copies the
    base workspace snapshot. ``diff_layer(scope)`` is the scope's copy compared
    against its base (parent copy, or the base snapshot for ground). Symlinks /
    non-regular files are rejected at diff time (UnsupportedOverlayEntryError),
    consistent with the overlay and clonefile carriers.
    """

    GROUND_SCOPE_ID = "ground"

    def __init__(
        self,
        workspace: Path,
        state_root: Path,
        *,
        base_lowerdir: Path | None = None,
        base_tree_oid: str | None = None,
    ) -> None:
        self._workspace = workspace.resolve()
        self._base_lowerdir = (base_lowerdir or workspace).resolve()
        self._base_tree_oid = base_tree_oid
        self._state_root = state_root.resolve()
        self._clones_root = self._state_root / "clones"
        self._base_tree_oid_path = self._state_root / "base-tree-oid"
        self._parents: dict[str, str | None] = {self.GROUND_SCOPE_ID: None}
        self._ensure_supported()
        self._state_root.mkdir(parents=True, exist_ok=True)
        self._clones_root.mkdir(parents=True, exist_ok=True)
        self._reset_if_base_changed()

    # --- CarrierBackend contract ---

    def create_layer(self, scope_id: str, *, parent_scope_id: str | None) -> None:
        if scope_id == self.GROUND_SCOPE_ID:
            parent_scope_id = None
        clone = self._clone_dir(scope_id)
        if scope_id != self.GROUND_SCOPE_ID and clone.exists():
            msg = f"Layer already exists for scope {scope_id!r}."
            raise RuntimeError(msg)
        self._parents[scope_id] = parent_scope_id
        self._clone_tree(self._source_dir(scope_id, parent_scope_id), clone)

    def has_layer(self, scope_id: str) -> bool:
        return self._clone_dir(scope_id).exists()

    def read_file(self, scope_id: str, path: str) -> bytes:
        return self.read_file_state(scope_id, path).content

    def read_file_state(self, scope_id: str, path: str) -> FileState:
        file_path = self._scope_file_path(scope_id, path)
        return FileState(file_path.read_bytes(), posix_to_git_mode(file_path.stat().st_mode))

    def write_file(self, scope_id: str, path: str, content: bytes, *, mode: int = 0o100644) -> None:
        file_path = self._scope_file_path(scope_id, path)
        file_path.parent.mkdir(parents=True, exist_ok=True)
        file_path.write_bytes(content)
        file_path.chmod(stat.S_IMODE(normalize_git_filemode(mode)))

    def delete_file(self, scope_id: str, path: str) -> None:
        file_path = self._scope_file_path(scope_id, path)
        if file_path.exists():
            file_path.unlink()

    def diff_layer(self, scope_id: str) -> list[tuple[str, bytes | None, int]]:
        clone = self._clone_dir(scope_id)
        if not clone.exists():
            return []
        return self._diff_dirs(new_dir=clone, base_dir=self._base_dir_for(scope_id))

    def commit_layer(self, scope_id: str, *, into_scope_id: str | None) -> None:
        if into_scope_id is None:
            msg = "commit_layer() requires an explicit destination layer."
            raise RuntimeError(msg)
        if scope_id == self.GROUND_SCOPE_ID:
            msg = "Ground layer cannot be committed into another layer."
            raise RuntimeError(msg)
        if not self.has_layer(into_scope_id):
            # Materialize the destination as a full tree first; applying a diff
            # into a fresh empty dir would leave a delta-only "working tree"
            # that the next fork would clone as if it were the whole workspace.
            # Source it from the destination's effective base (its parent's
            # copy if live, else the base snapshot) — the same one-level
            # fallback rule _source_dir/_base_dir_for apply everywhere else.
            # Routing through create_layer would crash on a destination whose
            # own parent record was already dropped (committed/discarded away).
            self._parents.setdefault(into_scope_id, None)
            self._clone_tree(self._base_dir_for(into_scope_id), self._clone_dir(into_scope_id))
        for path, content, mode in self.diff_layer(scope_id):
            if content is None:
                self.delete_file(into_scope_id, path)
            else:
                self.write_file(into_scope_id, path, content, mode=mode)
        self.discard_layer(scope_id)

    def discard_layer(self, scope_id: str) -> None:
        if scope_id == self.GROUND_SCOPE_ID:
            return
        self._parents.pop(scope_id, None)
        shutil.rmtree(self._clone_dir(scope_id), ignore_errors=True)

    def push_layer(self, scope_id: str | None = None) -> None:
        target_scope_id = scope_id or self.GROUND_SCOPE_ID
        if target_scope_id != self.GROUND_SCOPE_ID:
            msg = "Only the ground layer can be materialized."
            raise RuntimeError(msg)
        for path, content, mode in self.diff_layer(self.GROUND_SCOPE_ID):
            workspace_path = self._workspace_file_path(path)
            if content is None:
                if workspace_path.exists():
                    workspace_path.unlink()
                continue
            workspace_path.parent.mkdir(parents=True, exist_ok=True)
            workspace_path.write_bytes(content)
            workspace_path.chmod(stat.S_IMODE(normalize_git_filemode(mode)))
        self._reset_ground_layer()

    def working_path(self, scope_id: str) -> Path:
        return self._clone_dir(scope_id)

    def deactivate(self) -> None:
        # No mounts to release (layers are plain directories under state_root);
        # discard_layer / push_layer / _reset manage their lifetime.
        self._parents = {self.GROUND_SCOPE_ID: None}

    # --- internals ---

    def _ensure_supported(self) -> None:
        """Portable: no platform requirement. Subclasses may impose one."""
        return

    def _reset_if_base_changed(self) -> None:
        if self._base_tree_oid is None:
            return
        previous = self._base_tree_oid_path.read_text().strip() if self._base_tree_oid_path.exists() else None
        if previous == self._base_tree_oid:
            return
        shutil.rmtree(self._clones_root, ignore_errors=True)
        self._clones_root.mkdir(parents=True, exist_ok=True)
        self._parents = {self.GROUND_SCOPE_ID: None}
        self._base_tree_oid_path.write_text(self._base_tree_oid)

    def _reset_ground_layer(self) -> None:
        shutil.rmtree(self._clone_dir(self.GROUND_SCOPE_ID), ignore_errors=True)
        self._parents[self.GROUND_SCOPE_ID] = None
        self.create_layer(self.GROUND_SCOPE_ID, parent_scope_id=None)

    def _source_dir(self, scope_id: str, parent_scope_id: str | None) -> Path:
        if scope_id == self.GROUND_SCOPE_ID:
            return self._base_lowerdir
        if parent_scope_id is None:
            msg = f"Layer {scope_id!r} requires a parent layer."
            raise RuntimeError(msg)
        parent_clone = self._clone_dir(parent_scope_id)
        if parent_clone.exists():
            return parent_clone
        # A parent with no copy of its own works directly on the base snapshot
        # (ground, in normal operation). "A child scope copies its parent's
        # working tree" must hold there too: copy the base, so a body sees the
        # pre-existing workspace files rather than an empty tree.
        return self._base_lowerdir

    def _base_dir_for(self, scope_id: str) -> Path:
        parent = self._parents.get(scope_id)
        if parent is None:
            return self._base_lowerdir
        parent_clone = self._clone_dir(parent)
        return parent_clone if parent_clone.exists() else self._base_lowerdir

    def _clone_dir(self, scope_id: str) -> Path:
        return self._clones_root / scope_id

    def _clone_tree(self, source: Path, dest: Path) -> None:
        if dest.exists():
            shutil.rmtree(dest)
        dest.parent.mkdir(parents=True, exist_ok=True)
        if not source.exists():
            dest.mkdir(parents=True, exist_ok=True)
            return
        shutil.copytree(source, dest, symlinks=True)

    def _diff_dirs(self, *, new_dir: Path, base_dir: Path) -> list[tuple[str, bytes | None, int]]:
        # Trade-off: O(total files) and reads every file's bytes in BOTH dirs into
        # memory. A flat per-scope copy has no overlay "upper" to diff against, so
        # there is no cheap "unchanged" signal to skip a content read. Fine at the
        # small-workspace scale this floor targets; a size/mtime pre-filter is the
        # natural optimization if the copy carrier is ever used at larger scale.
        new = self._scan_dir(new_dir)
        base = self._scan_dir(base_dir)
        changes: list[tuple[str, bytes | None, int]] = []
        for rel in sorted(new):
            if base.get(rel) != new[rel]:
                content, mode = new[rel]
                changes.append((rel, content, mode))
        for rel in sorted(base):
            if rel not in new:
                changes.append((rel, None, 0))
        return changes

    def _scan_dir(self, root: Path) -> dict[str, tuple[bytes, int]]:
        files: dict[str, tuple[bytes, int]] = {}
        if not root.exists():
            return files
        for candidate in sorted(root.rglob("*")):
            rel = candidate.relative_to(root).as_posix()
            if not rel:
                continue
            file_stat = os.lstat(candidate)
            if stat.S_ISDIR(file_stat.st_mode):
                continue
            if stat.S_ISREG(file_stat.st_mode):
                files[rel] = (candidate.read_bytes(), posix_to_git_mode(file_stat.st_mode))
                continue
            kind = unsupported_overlay_entry_kind(file_stat.st_mode) or "unsupported"
            raise UnsupportedOverlayEntryError(path=rel, kind=kind)
        return files

    def _scope_file_path(self, scope_id: str, path: str) -> Path:
        clone = self._clone_dir(scope_id)
        if not clone.exists():
            msg = f"Layer {scope_id!r} is not available."
            raise RuntimeError(msg)
        return clone / self._normalize_relative_path(path)

    def _workspace_file_path(self, path: str) -> Path:
        return self._workspace / self._normalize_relative_path(path)

    def _normalize_relative_path(self, path: str) -> Path:
        pure = PurePosixPath(path)
        if not path or pure.is_absolute() or ".." in pure.parts:
            msg = f"Invalid workspace-relative path: {path!r}"
            raise ValueError(msg)
        normalized = Path(*pure.parts)
        if not normalized.parts:
            msg = f"Invalid workspace-relative path: {path!r}"
            raise ValueError(msg)
        return normalized
