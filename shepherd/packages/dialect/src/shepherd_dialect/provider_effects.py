"""Provider-neutral reconciliation of native file claims with carrier truth."""

from __future__ import annotations

import hashlib
import os
from collections.abc import Iterable, Mapping
from pathlib import Path


class ProviderEffectReconciliationError(RuntimeError):
    """Raised when the carrier tree cannot be captured safely."""


def snapshot_workspace_files(root: Path) -> Mapping[str, str]:
    """Hash the regular-file/symlink state below one canonical carrier root."""
    canonical = root.resolve()
    if not canonical.is_dir():
        raise ProviderEffectReconciliationError("provider carrier root must be an existing directory")
    snapshot: dict[str, str] = {}
    try:
        for directory, names, filenames in os.walk(canonical, followlinks=False):
            parent = Path(directory)
            for name in sorted((*names, *filenames)):
                path = parent / name
                relative = path.relative_to(canonical).as_posix()
                if path.is_symlink():
                    target = str(path.readlink()).encode("utf-8", errors="surrogateescape")
                    snapshot[relative] = _digest(b"symlink\0" + target)
                elif path.is_file():
                    hasher = hashlib.sha256()
                    hasher.update(b"file\0")
                    with path.open("rb") as stream:
                        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                            hasher.update(chunk)
                    snapshot[relative] = f"sha256:{hasher.hexdigest()}"
    except OSError as exc:
        raise ProviderEffectReconciliationError("provider carrier tree could not be captured") from exc
    return snapshot


def reconcile_provider_file_claims(
    *,
    before: Mapping[str, str],
    after: Mapping[str, str],
    provider_paths: Iterable[str],
) -> Mapping[str, object]:
    """Label provider attestations without creating authoritative file effects."""
    claims = tuple(sorted({_canonical_relative_path(path) for path in provider_paths}))
    carrier_paths = tuple(sorted(path for path in before.keys() | after.keys() if before.get(path) != after.get(path)))
    carrier_set = set(carrier_paths)
    provider_set = set(claims)
    records = [
        {
            "path": path,
            "classification": "carrier_confirmed" if path in carrier_set else "provider_only",
        }
        for path in claims
    ]
    records.extend(
        {"path": path, "classification": "carrier_only"} for path in carrier_paths if path not in provider_set
    )
    counts = {
        label: sum(1 for record in records if record["classification"] == label)
        for label in ("carrier_confirmed", "provider_only", "carrier_only")
    }
    return {
        "basis": "provider_claim_vs_carrier_tree",
        "complete": True,
        "provider_claim_count": len(claims),
        "carrier_changed_path_count": len(carrier_paths),
        "classification_counts": counts,
        "records": records,
    }


def completed_file_change_paths(activities: Iterable[object]) -> tuple[str, ...]:
    """Extract canonical relative paths from completed native file-change activities."""
    paths: set[str] = set()
    for activity in activities:
        if getattr(activity, "method", None) != "item/completed":
            continue
        payload = getattr(activity, "payload", {})
        if not isinstance(payload, Mapping) or payload.get("item_type") != "fileChange":
            continue
        values = payload.get("paths")
        if isinstance(values, list | tuple):
            for value in values:
                if isinstance(value, str):
                    paths.add(_canonical_relative_path(value))
    return tuple(sorted(paths))


def _canonical_relative_path(value: str) -> str:
    path = Path(value)
    if path.is_absolute() or not value or value in {".", ".."} or ".." in path.parts:
        raise ProviderEffectReconciliationError("provider file claim is not a canonical relative path")
    canonical = path.as_posix().removeprefix("./")
    if not canonical or canonical == ".":
        raise ProviderEffectReconciliationError("provider file claim is empty")
    return canonical


def _digest(value: bytes) -> str:
    return f"sha256:{hashlib.sha256(value).hexdigest()}"


__all__ = [
    "ProviderEffectReconciliationError",
    "completed_file_change_paths",
    "reconcile_provider_file_claims",
    "snapshot_workspace_files",
]
