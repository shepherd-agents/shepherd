"""File manifest for SimpleWorkspace state tracking.

This module provides FileEntry and FileManifest for tracking file state
without git. Uses stat-only scanning (size + mtime) for change detection.

Performance (validated by SW-01 spike):
- 10 files: ~2ms
- 100 files: ~18ms
- 1000 files: ~180ms
"""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path


@dataclass(frozen=True, slots=True)
class FileEntry:
    """Single file in a manifest.

    Uses stat-only data (size + mtime) for change detection.
    Content hashing is optional and only computed when needed.
    """

    path: str  # Relative path from workspace root
    size_bytes: int  # File size
    mtime_ns: int  # Modification time (nanoseconds)
    mode: int = 0o644  # File permissions
    content_hash: str | None = None  # Optional, computed on-demand

    @classmethod
    def from_path(
        cls,
        root: Path,
        rel_path: str,
        compute_hash: bool = False,
    ) -> FileEntry:
        """Create FileEntry from filesystem path.

        Args:
            root: Workspace root directory
            rel_path: Relative path from root
            compute_hash: Whether to compute content SHA256

        Returns:
            FileEntry with stat data and optional hash
        """
        full_path = root / rel_path
        stat = full_path.stat()

        content_hash = None
        if compute_hash:
            content = full_path.read_bytes()
            content_hash = hashlib.sha256(content).hexdigest()

        return cls(
            path=rel_path,
            size_bytes=stat.st_size,
            mtime_ns=stat.st_mtime_ns,
            mode=stat.st_mode & 0o777,
            content_hash=content_hash,
        )

    def has_changed(self, other: FileEntry) -> bool:
        """Detect changes using stat data (fast path).

        Returns True if size or mtime differs.
        """
        return self.size_bytes != other.size_bytes or self.mtime_ns != other.mtime_ns


@dataclass(frozen=True, slots=True)
class FileManifest:
    """Snapshot of workspace file state.

    This is the SimpleWorkspace equivalent of a git commit -
    a complete description of file state at a point in time.

    Uses stat-only scanning by default for performance.
    """

    entries: tuple[FileEntry, ...]
    created_at: datetime = field(
        default_factory=lambda: datetime.now(tz=UTC),
        compare=False,
        hash=False,
    )

    # Lookup cache (built lazily, not part of equality)
    _path_index: dict[str, FileEntry] | None = field(default=None, compare=False, hash=False)

    @classmethod
    def from_directory(
        cls,
        path: Path,
        exclude: set[str] | None = None,
        compute_hashes: bool = False,
    ) -> FileManifest:
        """Scan directory and create manifest.

        Args:
            path: Directory to scan
            exclude: Directory names to exclude (default: common non-content dirs)
            compute_hashes: Whether to compute content hashes

        Returns:
            FileManifest with all file entries
        """
        exclude = exclude or {
            ".git",
            "__pycache__",
            ".artifacts",
            ".venv",
            "node_modules",
            ".mypy_cache",
            ".pytest_cache",
            ".ruff_cache",
        }
        entries: list[FileEntry] = []

        for root_dir, dirs, files in os.walk(path):
            # Filter excluded directories in-place
            dirs[:] = [d for d in dirs if d not in exclude and not d.startswith(".")]

            for filename in files:
                if filename.startswith("."):
                    continue

                file_path = Path(root_dir) / filename
                try:
                    rel = str(file_path.relative_to(path))
                    entries.append(FileEntry.from_path(path, rel, compute_hash=compute_hashes))
                except OSError:
                    continue  # Skip files we can't stat

        return cls(entries=tuple(sorted(entries, key=lambda e: e.path)))

    def _build_index(self) -> dict[str, FileEntry]:
        """Build path lookup index (lazy, cached)."""
        if self._path_index is None:
            # Use object.__setattr__ to bypass frozen
            object.__setattr__(self, "_path_index", {e.path: e for e in self.entries})
        return self._path_index  # type: ignore

    def get(self, path: str) -> FileEntry | None:
        """Get entry by path. O(1) via index."""
        return self._build_index().get(path)

    def paths(self) -> frozenset[str]:
        """All paths in manifest."""
        return frozenset(e.path for e in self.entries)

    def detect_changes(self, other: FileManifest) -> tuple[set[str], set[str], set[str]]:
        """Detect added, modified, and removed files.

        Compares self (before) to other (after).

        Returns:
            (added, modified, removed) sets of file paths.
        """
        self_paths = self.paths()
        other_paths = other.paths()

        added = other_paths - self_paths
        removed = self_paths - other_paths

        modified = set()
        for path in self_paths & other_paths:
            self_entry = self.get(path)
            other_entry = other.get(path)
            if self_entry and other_entry and self_entry.has_changed(other_entry):
                modified.add(path)

        return added, modified, removed

    def __len__(self) -> int:
        """Number of files in manifest."""
        return len(self.entries)
