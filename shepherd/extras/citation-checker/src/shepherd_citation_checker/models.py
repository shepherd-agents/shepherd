"""Shared citation, source and assessment contracts."""

from typing import NotRequired, TypedDict


class Citation(TypedDict):
    """A supplied bibliography entry, preserved independently of interpretation."""

    id: str
    raw: str
    pages: NotRequired[list]


class SourceRecord(TypedDict):
    """A retained response; unavailable metadata is never synthesized."""

    url: str
    http_status: NotRequired[int]
    body: NotRequired[str]
    text: NotRequired[str]
    metadata: NotRequired[dict]
    error: NotRequired[str]


class Assessment(TypedDict):
    """Work identity, supplied-field findings, explanation and evidence."""

    id: str
    identity: dict
    field_checks: dict
    reason: str
    evidence: list[dict]


FIELD_ALIASES = {"author": "authors", "editor": "editors", "month": "publication_month", "page": "pages"}


def canonical_fields(values: dict) -> dict:
    """Normalize field vocabulary once; conflicting aliases invalidate the draft."""
    result = {}
    for field, value in values.items():
        field = FIELD_ALIASES.get(field, field)
        if field in result and result[field] != value:
            raise ValueError(f"Conflicting aliases for field {field}")
        result[field] = value
    return result
