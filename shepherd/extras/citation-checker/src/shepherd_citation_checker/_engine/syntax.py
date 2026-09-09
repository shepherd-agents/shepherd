"""Preserve supported partial facts without changing the frozen v2 verdict mapping."""

from typing import Any

from .contracts import placeholder_identifiers


def syntax_issues(raw) -> Any:
    result = []
    for value in placeholder_identifiers(raw):
        result.append(
            {
                "field": "identifier",
                "value": value,
                "code": "placeholder_arxiv_identifier",
                "explanation": "The supplied arXiv identifier contains placeholder characters.",
                "basis": "supplied citation syntax; not evidence that the work is fabricated",
            }
        )
    return result
