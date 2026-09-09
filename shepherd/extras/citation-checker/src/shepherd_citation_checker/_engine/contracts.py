"""Policy-v2 matching outcomes and evidence-contract validation."""

# ruff: noqa: TRY004

from __future__ import annotations

import json
import re
from typing import TYPE_CHECKING
from urllib.parse import urlparse

if TYPE_CHECKING:
    from pathlib import Path

SCHEMA_VERSION = "2.0"
POLICY_VERSION = "2.0"
STATUSES = {"matched", "matched_with_errors", "no_match_found", "ambiguous_match", "check_incomplete"}
DECIDED = {"matched", "matched_with_errors", "no_match_found"}
PENDING = {"ambiguous_match", "check_incomplete"}
DISPLAY = {s: s.replace("_", " ").capitalize() for s in STATUSES}
FIELDS = {"title", "authors", "venue", "year", "identifier", "version", "pages", "publisher", "other"}
SEARCH_KINDS = {"identifier", "title_authors", "alternatives", "official_records"}


def placeholder_identifiers(raw: str) -> list[str]:
    return [m[0] for m in re.finditer(r"\barxiv\s*:\s*\d{4}\.[Xx?]{2,}", raw, re.IGNORECASE)]


def _text(value: object, field: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be a nonempty string")


def evidence_record(source: dict, root: Path, *, allow_missing: bool = False) -> dict:
    """Check retained source identity/content, not semantic support."""
    if not isinstance(source, dict):
        raise ValueError("Evidence must be an object")
    url, filename = source.get("url"), source.get("evidence_file")
    _text(url, "Evidence URL")
    _text(filename, "Evidence file")
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("Evidence URL must be absolute HTTP(S)")
    path = (root / filename).resolve()
    if not path.is_relative_to((root / "evidence").resolve()) or not path.is_file():
        raise ValueError("Evidence file missing or outside evidence directory")
    record = json.loads(path.read_text())
    if not isinstance(record, dict) or record.get("url") != url:
        raise ValueError("Evidence URL differs from retained source")
    if not any(record.get(k) for k in ("body", "text", "metadata", "candidates")):
        raise ValueError("Evidence record has no retained content")
    # Prefetched responses carry an HTTP status. Model web-search records can
    # instead be identified explicitly; they are not independent HTTP recordings.
    status = record.get("http_status")
    if status is not None:
        if type(status) is not int or not (200 <= status < 300 or (allow_missing and status == 404)):
            raise ValueError("Failed HTTP response cannot support a decision")
    elif record.get("source_type") != "web_search":
        raise ValueError("Evidence needs successful HTTP status or web_search source type")
    return record
