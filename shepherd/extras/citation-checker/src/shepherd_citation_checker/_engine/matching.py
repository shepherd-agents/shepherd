"""Conservative positive-only acceptance; uncertainty always routes to Opus."""

import re
import unicodedata
from typing import Any
from urllib.parse import urlparse

from .claims import comparison_key
from .response_health import payload_error, usable
from .sources import candidates
from .validation import unresolved


def normalize(value) -> Any:
    # Preserve words, order, digits and mathematical signs; do not fuzzy-match.
    value = unicodedata.normalize("NFKC", str(value)).casefold()
    return " ".join(value.replace("\u2013", "-").replace("\u2014", "-").split()).rstrip(".")


def parse_fields(raw) -> Any:
    """Only fully labelled input is unambiguous in the first implementation."""
    aliases = {"author": "authors", "journal": "venue", "booktitle": "venue", "page": "pages"}
    allowed = {"title", "authors", "year", "venue", "publisher", "doi", "pages", "volume", "issue"}
    fields = {}
    for line in raw.strip().splitlines():
        match = re.fullmatch(r"([A-Za-z]+)\s*:\s*(\S.*)", line.strip())
        if not match:
            return None
        key, value = match.groups()
        key = aliases.get(key.lower(), key.lower())
        if key not in allowed or key in fields:
            return None
        fields[key] = value.strip()
    if not {"title", "authors", "year", "venue"} <= fields.keys():
        return None
    if not re.fullmatch(r"(?:19|20)\d{2}", fields["year"]):
        return None
    if re.search(r"et\s+al\b|\.\.\.|…|https?://", fields["authors"], re.IGNORECASE):
        return None
    if "doi" in fields and not re.fullmatch(r"10\.\d{4,9}/\S+", fields["doi"], re.IGNORECASE):
        return None
    return fields


def one(value) -> Any:
    if isinstance(value, list):
        return value[0] if len(value) == 1 else None
    return value


def field_matches(field, supplied, actual) -> Any:
    if actual is None:
        return False
    if normalize(supplied) == normalize(actual):
        return True
    # Accept only abbreviations explicitly supplied by the source, never inferred acronyms.
    return (
        field == "venue"
        and bool(re.fullmatch(r"[A-Z][A-Z0-9/-]{1,11}", supplied))
        and supplied in re.findall(r"\(([A-Z][A-Z0-9/-]{1,11})\)", str(actual))
    )


def author_names(item) -> Any:
    names = []
    for author in item.get("author", []):
        if not isinstance(author, dict):
            return []
        if author.get("given") and author.get("family"):
            names.append(normalize(author["given"] + " " + author["family"]))
        elif author.get("literal"):
            names.append(normalize(author["literal"]))
        else:
            return []
    return names


def authors_match(raw, item) -> Any:
    authors = item.get("author", [])
    if not authors or any(not isinstance(a, dict) for a in authors):
        return False
    full = author_names(item)
    if not full:
        return False
    supplied = raw.strip().removeprefix("& ")
    parts = supplied.split(";")
    # One dataset serializes `Surname; A. B.; Surname; C.` rather than names.
    if len(parts) == 2 * len(authors) and all(re.fullmatch(r"\s*(?:[A-Za-z]\.?\s*)+", p) for p in parts[1::2]):
        parts = [parts[i + 1].strip() + " " + parts[i].strip().removeprefix("& ") for i in range(0, len(parts), 2)]
    elif ";" not in supplied:
        parts = re.split(r",\s*(?:and\s+)?|\s+and\s+", supplied)
    if len(parts) != len(authors):
        return False
    for supplied_name, author, actual in zip(parts, authors, full, strict=True):
        supplied_name = normalize(supplied_name.strip().removeprefix("& "))
        if supplied_name == actual:
            continue
        if not author.get("given") or not author.get("family"):
            return False
        initials = " ".join(w[0] for w in re.findall(r"[^\W\d_]+", author["given"], re.UNICODE))
        short = normalize(initials + " " + author["family"])
        # Only initials can shorten given names; never substitute another full name.
        if normalize(supplied_name.replace(".", " ")) != short:
            return False
    return True


def parse_prose(raw, title) -> Any:
    """Consume a complete simple bibliography grammar, anchored on a candidate title."""
    if not isinstance(title, str) or "\n" in raw or re.search(r"et\s+al\b|https?://|10\.\d{4,9}/", raw, re.IGNORECASE):
        return None
    found = re.fullmatch(
        r"(?P<authors>.+?)\.\s+" + re.escape(title.rstrip(".")) + r"\.\s+(?P<venue>.+?),\s*(?P<year>(?:19|20)\d{2})\.?",
        raw.strip(),
        re.IGNORECASE,
    )
    return {"title": title, **found.groupdict()} if found else None


def match(ref, sources) -> Any:
    try:
        return _match(ref, sources)
    except (AttributeError, IndexError, KeyError, TypeError, ValueError):
        # Malformed external metadata must never turn into acceptance or abort routing.
        return None, "Malformed citation or registry metadata requires review"


def _match(ref, sources) -> Any:
    from .symbolic_arxiv import match_arxiv

    arxiv = match_arxiv(ref, sources)
    if arxiv:
        return arxiv, "All supplied fields verified against the authoritative arXiv record"
    fields = parse_fields(ref["raw"])
    all_candidates = []
    for sid, source in sources.items():
        record = source["record"]
        # Start with authoritative Crossref records; other formats go to Opus.
        if urlparse(record["url"]).hostname != "api.crossref.org" or not usable(record):
            continue
        for item in candidates(record):
            all_candidates.append((sid, item))
    if fields is None:
        parsed = [f for _, item in all_candidates if (f := parse_prose(ref["raw"], one(item.get("title"))))]
        if not parsed or any(f != parsed[0] for f in parsed):
            return None, "Citation fields cannot be parsed completely and unambiguously"
        fields = parsed[0]
    relevant = [
        (sid, item) for sid, item in all_candidates if normalize(one(item.get("title"))) == normalize(fields["title"])
    ]
    if fields.get("doi") and any(
        normalize(item.get("DOI")) == normalize(fields["doi"])
        and normalize(one(item.get("title"))) != normalize(fields["title"])
        for _, item in all_candidates
    ):
        return None, "Supplied identifier has conflicting title evidence"
    if not relevant:
        return None, "No exact-title authoritative candidate"
    accepted = []
    for sid, item in relevant:
        # No mixing versions or accepting an arbitrary date among conflicting ones.
        dates = [
            item[k].get("date-parts", []) for k in ("published", "published-print", "published-online") if item.get(k)
        ]
        years = {str(d[0][0]) for d in dates if d and d[0]}
        values = {
            "title": one(item.get("title")),
            "venue": one(item.get("container-title")),
            "year": next(iter(years)) if len(years) == 1 else None,
            "publisher": item.get("publisher"),
            "doi": item.get("DOI"),
            "pages": item.get("page"),
            "volume": item.get("volume"),
            "issue": item.get("issue"),
        }
        if not authors_match(fields["authors"], item) or any(
            not field_matches(k, v, values.get(k)) for k, v in fields.items() if k != "authors"
        ):
            return None, "An exact-title candidate has conflicting or incomplete supplied metadata"
        if not item.get("DOI"):
            return None, "Matching record has no stable identifier"
        accepted.append((sid, item))
    if len({normalize(i["DOI"]) for _, i in accepted}) != 1:
        return None, "Multiple candidate identifiers; version/identity needs review"
    sid, item = accepted[0]
    row = unresolved(
        ref, "All supplied citation fields match one authoritative Crossref record after conservative normalization."
    )
    checks = {
        ("identifier" if k == "doi" else k): {
            "outcome": "verified",
            "explanation": "Matches the retained record.",
            "evidence": [sid],
        }
        for k in fields
    }
    row.update(
        identity={
            "state": "identified",
            "explanation": "Exact title and complete ordered authors match; all supplied metadata agrees.",
            "supporting_fields": list(checks),
            "evidence": [sid],
        },
        matched_title=one(item["title"]),
        field_checks=checks,
        evidence=[
            {
                "source_id": sid,
                "supports": "One record supports every supplied field; no competing exact-title identifier.",
            }
        ],
        correction_note="No correction needed; accepted by deterministic checks.",
    )
    row["investigation"].update(
        sufficient=True,
        scope="Conservative comparison with retained Crossref candidates; no claim of exhaustive search.",
    )
    return row, "All supplied fields verified against one unambiguous record"


def complete_optional(result, sources, identity_ids) -> Any:
    """Never decide identity, overwrite errors, infer dates, or expand author names."""
    checks = result["field_checks"]
    if result["identity"]["state"] != "identified" or result.get("claim_policy") != "supplied-claims-1":
        return
    if any(checks.get(f, {}).get("outcome") != "verified" for f in ("title", "authors")):
        return

    def title_key(value) -> Any:
        if isinstance(value, list):
            value = value[0] if value else ""
        return comparison_key("title", str(value)).rstrip(".")

    title = title_key(result["matched_title"])
    records = []
    for sid in identity_ids:
        record = sources[sid]["record"]
        if payload_error(record) or record.get("http_status") != 200:
            continue
        matches = [c for c in candidates(record) if isinstance(c, dict) and title_key(c.get("title", "")) == title]
        # A title shared by multiple candidate records cannot anchor completion.
        if len(matches) > 1:
            return
        if len(matches) == 1:
            records.append((sid, matches[0]))
    dois = {c["DOI"].casefold() for _, c in records if isinstance(c.get("DOI"), str) and c["DOI"]}
    if len(dois) > 1:
        return
    for field, key in (
        ("location", "event.location"),
        ("volume", "volume"),
        ("issue", "issue"),
        ("issn", "ISSN"),
        ("isbn", "ISBN"),
    ):
        check = checks.get(field, {})
        claim = result.get("claim_spans", {}).get(field)
        if check.get("outcome") != "uncertain" or not claim:
            continue
        values = []
        for sid, candidate in records:
            event = candidate.get("event")
            value = (
                (event.get("location") if isinstance(event, dict) else None)
                if key == "event.location"
                else candidate.get(key)
            )
            if isinstance(value, list):
                # Multiple serial identifiers need edition/medium interpretation.
                value = value[0] if len(value) == 1 else None
            if isinstance(value, (str, int)) and str(value).strip():
                values.append((sid, str(value)))
        if not values or {comparison_key(field, v) for _, v in values} != {comparison_key(field, claim)}:
            continue
        ids = list(dict.fromkeys(sid for sid, _ in values))
        check.update(
            outcome="verified",
            evidence=ids,
            explanation=f"Exact supplied {field} matches the identified work's structured {key}.",
        )
        result.setdefault("deterministic_completions", []).append(
            {
                "field": field,
                "claim": claim,
                "source_ids": ids,
                "source_field": key,
                "values": [v for _, v in values],
                "previous_outcome": "uncertain",
            }
        )
