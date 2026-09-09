"""Positive-only checks for fully consumed arXiv bibliography entries."""

import re
import unicodedata
from typing import Any
from urllib.parse import urlparse

from .response_health import usable
from .validation import unresolved

MONTHS = [
    "January",
    "February",
    "March",
    "April",
    "May",
    "June",
    "July",
    "August",
    "September",
    "October",
    "November",
    "December",
]
ARXIV_ID = r"\d{4}\.\d{4,5}"


def flat(value) -> Any:
    value = unicodedata.normalize("NFKC", value).translate(str.maketrans({"\u2019": "'", "\u2013": "-", "\u2014": "-"}))
    # Preserve a printed hyphen; do not guess whether it was discretionary.
    value = re.sub(r"-\s*\n\s*", "-", value)
    return " ".join(value.split()).casefold()


def name(value) -> Any:
    return flat(value).replace(".", "")


def match_arxiv(ref, sources) -> Any:
    """Require title, complete ordered names, date and all identifiers to agree.

    An exact identifier alone never validates the citation. Unknown suffixes,
    omitted authors, specific versions and venue claims still require review.
    """
    raw = flat(ref["raw"])
    # Join whitespace only inside a recognized arXiv URL, including PDF wraps.
    raw = re.sub(
        r"https?\s*:\s*//\s*arxiv\.\s*org/abs/\s*\d{4}\.\s*\d{4,5}(?:v\d+)?",
        lambda m: re.sub(r"\s+", "", m[0]),
        raw,
    )
    accepted = []
    signatures = {}
    for sid, source in sources.items():
        record = source["record"]
        url = urlparse(record.get("resolved_url") or record.get("url", ""))
        if url.hostname != "arxiv.org" or not re.fullmatch(r"/abs/" + ARXIV_ID, url.path):
            continue
        if not usable(record) or record.get("truncated"):
            continue
        # Parse the retained page itself, not an unaudited derived metadata field.
        from .fetch import Metadata

        parser = Metadata()
        parser.feed(record.get("body", ""))
        meta = parser.values
        if any(len(meta.get(key, [])) != 1 for key in ("citation_title", "citation_arxiv_id", "citation_date")):
            continue
        identifier = meta["citation_arxiv_id"][0]
        if identifier != url.path.rsplit("/", 1)[1]:
            continue
        signature = (meta["citation_title"][0], tuple(meta.get("citation_author", [])), meta["citation_date"][0])
        if identifier in signatures and signatures[identifier] != signature:
            return None  # Different retained versions require an explicit comparison.
        signatures[identifier] = signature
        title = meta["citation_title"][0]
        grammar = (
            r"(?P<authors>.+?)\.\s+"
            + re.escape(flat(title).rstrip("."))
            + r"[.,]\s+(?:(?P<month>"
            + "|".join(m.casefold() for m in MONTHS)
            + r")\s+)?(?P<year>(?:19|20)\d{2})\.\s+url\s+https?://arxiv\.org/abs/"
            + re.escape(identifier)
            + r"\s*\.?(?:\s+arxiv:"
            + re.escape(identifier)
            + r"(?:\s+\[(?P<subject>[a-z-]+)\])?\s*\.)?"
        )
        parsed = re.fullmatch(grammar, raw)
        if not parsed:
            continue
        date = re.fullmatch(r"((?:19|20)\d{2})/(\d{2})/(\d{2})", meta["citation_date"][0])
        if not date or date[1] != parsed["year"]:
            continue
        if parsed["month"] and int(date[2]) != [m.casefold() for m in MONTHS].index(parsed["month"]) + 1:
            continue
        if parsed["subject"] and not re.search(r"\b" + re.escape(parsed["subject"]) + r"\.[A-Z]{2}\b", record["body"]):
            continue
        actual = []
        for author in meta.get("citation_author", []):
            parts = author.split(", ")
            actual.append(name(parts[1] + " " + parts[0]) if len(parts) == 2 else name(author))
        supplied = re.split(r",\s*(?:and\s+)?|\s+and\s+", parsed["authors"])
        if not actual or [name(n) for n in supplied] != actual:
            continue
        accepted.append((sid, title, identifier))
    if not accepted:
        return None
    if len({(flat(title), identifier) for _, title, identifier in accepted}) != 1:
        return None
    sid, title, _ = accepted[0]
    explanation = "Title, complete ordered authors, first-posted date and all supplied arXiv identifiers match the retained arXiv record."
    row = unresolved(ref, explanation)
    fields = ["title", "authors", "year", "identifier"]
    row.update(
        identity={"state": "identified", "explanation": explanation, "supporting_fields": fields, "evidence": [sid]},
        matched_title=title,
        field_checks={
            k: {"outcome": "verified", "explanation": "Matches the retained arXiv record.", "evidence": [sid]}
            for k in fields
        },
        evidence=[{"source_id": sid, "supports": explanation}],
        correction_note="No correction needed; accepted by deterministic checks.",
    )
    row["investigation"].update(
        sufficient=True,
        scope="Complete comparison of an unversioned arXiv citation with its authoritative record.",
        limitations=[],
    )
    return row
