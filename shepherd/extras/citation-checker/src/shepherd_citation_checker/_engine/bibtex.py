"""Format proposed corrections from structured, evidence-linked audit metadata.

This module makes no network or model calls. Source selection and interpretation
remain the reviewer's responsibility; validation does not prove semantic support.
"""

from __future__ import annotations

import re
from urllib.parse import urlparse

REQUIRED_FIELDS = {"article": "journal", "inproceedings": "booktitle", "book": "publisher", "misc": None}
OPTIONAL_FIELDS = {
    "journal",
    "booktitle",
    "publisher",
    "volume",
    "number",
    "pages",
    "doi",
    "url",
    "isbn",
    "edition",
    "address",
    "eprint",
    "archiveprefix",
    "primaryclass",
}
ESCAPES = {
    "\\": r"{\textbackslash}",
    "{": r"{\char123}",
    "}": r"{\char125}",
    "&": r"\&",
    "%": r"\%",
    "$": r"\$",
    "#": r"\#",
    "_": r"\_",
    "~": r"{\textasciitilde}",
    "^": r"{\textasciicircum}",
}


def _text(value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("Citation fields must be nonempty plain-text strings")
    return " ".join(value.split())


def _escape(value: str) -> str:
    return "".join(ESCAPES.get(char, char) for char in _text(value))


def _normalized_title(value: object) -> str:
    return re.sub(r"[\W_]+", "", _text(value).casefold())


def citation_key(identifier: str) -> str:
    """Preserve numeric reference numbers; encode other IDs without collisions."""
    return "ref_" + (identifier if re.fullmatch(r"[0-9]+", identifier) else "x" + identifier.encode().hex())


def render_citation(citation: dict, identifier: str) -> str:
    """Render a restricted schema as one entry, with no executable TeX input."""
    kind = citation.get("entry_type")
    if not isinstance(kind, str) or kind not in REQUIRED_FIELDS:
        raise ValueError("Unsupported citation type")
    title = _text(citation.get("title"))
    year = _text(citation.get("year"))
    if not re.fullmatch(r"[12][0-9]{3}", year):
        raise ValueError("A supported publication year is required")
    authors = citation.get("authors")
    if not isinstance(authors, list) or not authors:
        raise ValueError("A complete author list is required")
    names = []
    for author in authors:
        if not isinstance(author, dict):
            raise TypeError("Authors must have family/given or literal names")
        if set(author) == {"literal"}:
            name = _text(author["literal"])
            formatted = "{" + _escape(name) + "}"
        elif set(author) <= {"family", "given"} and "family" in author:
            family = _text(author["family"])
            given = _text(author["given"]) if "given" in author else ""
            name = f"{family}, {given}" if given else family
            formatted = _escape(name)
        else:
            raise ValueError("Authors must have family/given or literal names")
        if re.search(r"\bet\s+al\b|\bothers\b", name, re.IGNORECASE):
            raise ValueError("Truncated author lists cannot be exported")
        # BibTeX's name separator must not be smuggled into a personal name.
        if "literal" not in author and re.search(r"\band\b", name, re.IGNORECASE):
            raise ValueError("Use a literal name for a group author")
        names.append(formatted)
    fields = citation.get("fields", {})
    if not isinstance(fields, dict) or set(fields) - OPTIONAL_FIELDS:
        raise ValueError("Unsupported BibTeX field")
    fields = {key: _text(value) for key, value in fields.items()}
    required = REQUIRED_FIELDS[kind]
    if required and required not in fields:
        raise ValueError(f"{kind} requires {required}")
    if "url" in fields and urlparse(fields["url"]).scheme not in {"https", "http"}:
        raise ValueError("Citation URL must be HTTP(S)")
    if "doi" in fields and not re.fullmatch(r"10\.[0-9]{4,9}/\S+", fields["doi"]):
        raise ValueError("Invalid DOI syntax")
    values = {"title": "{" + _escape(title) + "}", "author": " and ".join(names), "year": year}
    values.update({key: _escape(value) for key, value in sorted(fields.items())})
    lines = [f"@{kind}{{{citation_key(identifier)},"]
    lines.extend(f"  {key} = {{{value}}}," for key, value in values.items())
    return "\n".join([*lines, "}"])
