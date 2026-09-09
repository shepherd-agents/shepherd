"""Atomic run artifacts and content hashes."""

import hashlib
import json
from pathlib import Path
from typing import Any


def read(path) -> Any:
    """Read a JSON artifact."""
    return json.loads(Path(path).read_text())


def write(path, value) -> Any:
    """Atomically replace a JSON artifact."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")
    temporary.replace(path)


def digest(path) -> Any:
    """Return a file content SHA-256 digest."""
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()
