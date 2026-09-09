"""Validate bounded page sets from retained responses, without inventing hits."""

import json
import re
from collections import defaultdict
from typing import Any
from urllib.parse import parse_qs, parse_qsl, urlencode, urlparse, urlunparse

from .arxiv_atom import arxiv_view, is_arxiv_api
from .response_health import usable


def page_key(record) -> Any:
    url = urlparse(record["url"])
    ignored = {
        "api.crossref.org": {"rows", "offset"},
        "api.openalex.org": {"per-page", "per_page", "page"},
        "api.datacite.org": {"page[size]", "page[number]"},
        "dblp.org": {"h", "f", "c"},
        "dblp.uni-trier.de": {"h", "f", "c"},
    }.get(url.hostname, set())
    if is_arxiv_api(record["url"]):
        ignored = {"start", "max_results"}
    # Cursor pages need explicit chain verification, not offset inference.
    if "cursor" in parse_qs(url.query):
        ignored = set()
    query = sorted((k, v) for k, v in parse_qsl(url.query, keep_blank_values=True) if k not in ignored)
    return urlunparse(url._replace(query=urlencode(query), fragment=""))


def page_offset(record) -> Any:
    url = urlparse(record["url"])
    if is_arxiv_api(record["url"]):
        view = arxiv_view(record)
        return view["start"] if view else None
    query = parse_qs(url.query)
    if "cursor" in query:
        return None
    try:
        if url.hostname == "api.crossref.org":
            return int(query.get("offset", [0])[0])
        if url.hostname == "api.openalex.org":
            size = int(query.get("per-page", query.get("per_page", [25]))[0])
            return (int(query.get("page", [1])[0]) - 1) * size
        if url.hostname == "api.datacite.org":
            return (int(query.get("page[number]", [1])[0]) - 1) * int(query.get("page[size]", [25])[0])
        if url.hostname in {"dblp.org", "dblp.uni-trier.de"}:
            return int(json.loads(record["body"])["result"]["hits"].get("@first", 0))
    except (ValueError, KeyError, TypeError):
        return None
    return 0


def complete_groups(records) -> Any:
    """Return independently complete query groups; reject gaps and changed totals."""
    from .sources import candidates, search_extent

    grouped = defaultdict(list)
    for record in records:
        grouped[page_key(record)].append(record)
    summaries = []
    for key, pages in grouped.items():
        total, slots, seen = None, {}, {}
        for record in pages:
            extent = search_extent(record)
            offset = page_offset(record)
            if (
                not usable(record)
                or record.get("truncated")
                or not extent
                or offset is None
                or offset < 0
                or not extent["candidate_metadata_available"]
                or any(not str(w).startswith("Nonzero first hit") for w in extent["translation_warnings"])
            ):
                raise ValueError("Unusable, truncated, translated or unsupported search page")
            count = extent["total_hits"]
            if count < 0 or offset > count or (total is not None and total != count):
                raise ValueError("Result totals changed between pages")
            total = count
            items = candidates(record)
            if len(items) != extent["retained_hits"]:
                raise ValueError("Every retained hit needs candidate metadata")
            for n, item in enumerate(items, offset):
                identity = str(item["candidate_id"])
                fingerprint = json.dumps(item, sort_keys=True)
                if n >= total or (n in slots and slots[n] != fingerprint):
                    raise ValueError("Overlapping pages conflict or exceed the reported total")
                if identity in seen and seen[identity] != n:
                    raise ValueError("Duplicate candidate IDs cannot fill a missing page")
                slots[n], seen[identity] = fingerprint, n
        if total is None or len(slots) != total or (slots and (min(slots) != 0 or max(slots) != total - 1)):
            raise ValueError("Search page set is incomplete; narrow the query or fetch missing pages")
        summaries.append({"query": key, "total_hits": total, "unique_candidates": len(seen), "pages": len(pages)})
    if not summaries:
        raise ValueError("Search coverage requires retained pages")
    return summaries


def official_scope(record) -> Any:
    """Recognize actual registry filters and parsed official proceedings only."""
    from .proceedings import proceedings_view

    url = urlparse(record["url"])
    query = parse_qs(url.query)
    filters = ",".join(query.get("filter", []))
    if is_arxiv_api(record["url"]):
        return bool(query.get("search_query") and arxiv_view(record))
    if url.hostname == "api.crossref.org":
        return bool(re.fullmatch(r"/(?:v1/)?journals/\d{4}-\d{3}[\dXx]/works/?", url.path)) or (
            url.path.rstrip("/") in {"/works", "/v1/works"}
            and any(
                field.startswith(("issn:", "isbn:")) and field.split(":", 1)[1].strip() for field in filters.split(",")
            )
        )
    if url.hostname == "api.openalex.org":
        return url.path.rstrip("/") == "/works" and any(
            field.startswith(("primary_location.source.id:", "locations.source.id:", "source.issn:"))
            and field.split(":", 1)[1].strip()
            for field in filters.split(",")
        )
    if url.hostname in {"www.ebi.ac.uk", "europepmc.org"}:
        return bool(re.search(r'\bJOURNAL\s*:\s*"[^\"]+"', " ".join(query.get("query", [])), re.IGNORECASE))
    view = proceedings_view(record)
    return bool(view and view["complete"])
