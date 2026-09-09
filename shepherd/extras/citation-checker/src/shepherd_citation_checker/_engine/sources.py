"""Compact evidence presentation and source-checked report expansion."""

# ruff: noqa: TRY004
import copy
import hashlib
import json
import re
import unicodedata
from typing import Any
from urllib.parse import parse_qs, urlparse

from .arxiv_atom import arxiv_view
from .proceedings import proceedings_view
from .response_health import payload_error


def source_id(record) -> Any:
    return "s" + hashlib.sha256(json.dumps(record, sort_keys=True).encode()).hexdigest()[:16]


def candidates(record) -> Any:
    """Project actual registry responses; preserve identity and metadata fields."""
    atom = arxiv_view(record)
    if atom:
        return atom["candidates"]
    proceedings = proceedings_view(record)
    if proceedings:
        return proceedings["candidates"]
    try:
        body = json.loads(record.get("body", ""))
    except (ValueError, TypeError):
        body = {}
    if not isinstance(body, dict):
        return record.get("candidates", [])
    host = urlparse(record.get("url", "")).hostname
    if host == "api.crossref.org" and isinstance(body.get("message"), dict):
        data = body["message"]
        items = data.get("items", [data])
        keys = (
            "title",
            "author",
            "container-title",
            "published",
            "published-print",
            "published-online",
            "DOI",
            "URL",
            "type",
            "page",
            "volume",
            "issue",
            "publisher",
            "event",
            "editor",
            "ISSN",
            "ISBN",
            "edition",
        )
        return [
            {"candidate_id": str(i.get("DOI") or n), **{k: i[k] for k in keys if k in i}}
            for n, i in enumerate(items)
            if isinstance(i, dict) and i.get("title")
        ]
    if host == "api.openalex.org" and isinstance(body.get("results"), list):
        return [
            {
                "candidate_id": str(i.get("id") or n),
                "title": i.get("display_name"),
                "authors": [a.get("author", {}).get("display_name") for a in i.get("authorships", [])],
                "year": i.get("publication_year"),
                "doi": i.get("doi"),
                "venue": (i.get("primary_location") or {}).get("source"),
                "type": i.get("type"),
            }
            for n, i in enumerate(body["results"])
        ]
    if host == "api.datacite.org" and isinstance(body.get("data"), (dict, list)):
        data = body["data"] if isinstance(body["data"], list) else [body["data"]]
        result = []
        for item in data:
            a = item.get("attributes", {})
            if not a.get("titles"):
                continue
            result.append(
                {
                    "candidate_id": str(item.get("id", a.get("doi"))),
                    "title": [t.get("title") for t in a["titles"]],
                    "author": [
                        {"given": x.get("givenName"), "family": x.get("familyName"), "name": x.get("name")}
                        for x in a.get("creators", [])
                    ],
                    "DOI": a.get("doi"),
                    "year": a.get("publicationYear"),
                    "publisher": a.get("publisher"),
                    "container": a.get("container"),
                    "URL": a.get("url"),
                    "version": a.get("version"),
                    "dates": a.get("dates"),
                }
            )
        return result
    if host in {"www.ebi.ac.uk", "europepmc.org"} and "hitCount" in body:
        return [
            {"candidate_id": str(i.get("source", "")) + ":" + str(i.get("id")), **i}
            for i in body.get("resultList", {}).get("result", [])
        ]
    if host in {"dblp.org", "dblp.uni-trier.de"}:
        hits = body.get("result", {}).get("hits", {}).get("hit", [])
        if isinstance(hits, dict):
            hits = [hits]
        return [{"candidate_id": str(hit.get("@id", n)), **hit.get("info", {})} for n, hit in enumerate(hits)]
    return record.get("candidates", [])


def search_extent(record) -> Any:
    """Only recognize independently countable JSON search responses."""
    atom = arxiv_view(record)
    if atom:
        return {
            "total_hits": atom["total"],
            "retained_hits": len(atom["candidates"]),
            "query": parse_qs(urlparse(record["url"]).query),
            "translation_warnings": ["Nonzero first hit; combine verified pages"] if atom["start"] else [],
            "candidate_metadata_available": True,
            "complete_page_set": atom["start"] == 0 and len(atom["candidates"]) == atom["total"],
        }
    proceedings = proceedings_view(record)
    if proceedings:
        return {
            "total_hits": len(proceedings["candidates"]),
            "retained_hits": len(proceedings["candidates"]),
            "query": {},
            "translation_warnings": [],
            "candidate_metadata_available": True,
            "complete_page_set": True,
            "official_scope": proceedings["scope"],
        }
    try:
        body = json.loads(record.get("body", ""))
    except (ValueError, TypeError):
        return None
    if not isinstance(body, dict):
        return None
    url = urlparse(record.get("url", ""))
    query = parse_qs(url.query)
    count, returned, warnings = None, None, []
    metadata_available = True
    if url.hostname == "api.crossref.org" and isinstance(body.get("message"), dict):
        data = body["message"]
        if "items" in data:
            count, returned = data.get("total-results"), len(data["items"])
            if query.get("offset", ["0"])[0] != "0":
                warnings.append("Nonzero first hit; combine verified pages before claiming completeness")
    elif url.hostname == "api.openalex.org" and isinstance(body.get("results"), list):
        count, returned = body.get("meta", {}).get("count"), len(body["results"])
    elif url.hostname == "api.datacite.org" and isinstance(body.get("data"), list):
        count, returned = body.get("meta", {}).get("total"), len(body["data"])
    elif url.hostname in {"www.ebi.ac.uk", "europepmc.org"} and "hitCount" in body:
        count, returned = body["hitCount"], len(body.get("resultList", {}).get("result", []))
    elif url.hostname in {"dblp.org", "dblp.uni-trier.de"} and isinstance(body.get("result", {}).get("hits"), dict):
        data = body["result"]["hits"]
        count, returned = int(data.get("@total", 0)), len(candidates(record))
        if int(data.get("@first", 0)) != 0:
            warnings.append("Nonzero first hit; prior page not retained in this source")
    elif url.hostname == "eutils.ncbi.nlm.nih.gov" and isinstance(body.get("esearchresult"), dict):
        data = body["esearchresult"]
        count, returned = int(data.get("count", 0)), len(data.get("idlist", []))
        warnings = data.get("warninglist", {}).get("quotedphrasesnotfound", [])
        metadata_available = count == 0
    if type(count) is not int or type(returned) is not int:
        return None
    return {
        "total_hits": count,
        "retained_hits": returned,
        "query": query,
        "translation_warnings": warnings,
        "candidate_metadata_available": metadata_available,
        "complete_page_set": returned >= count
        and not warnings
        and metadata_available
        and not record.get("truncated", False),
    }


def catalog(root) -> Any:
    result = {}
    for path in sorted((root / "evidence").rglob("*.json")):
        if path.is_symlink() or not path.resolve().is_relative_to((root / "evidence").resolve()):
            raise ValueError("Evidence path escapes its directory")
        record = json.loads(path.read_text())
        if not isinstance(record, dict) or not isinstance(record.get("url"), str):
            raise ValueError("Evidence must retain its source URL")
        result[source_id(record)] = {"record": record, "evidence_file": path.relative_to(root).as_posix()}
    return result


def compact(record) -> Any:
    from .notes import bibliographic_notes

    out = {"source_id": source_id(record), "url": record["url"], "http_status": record.get("http_status")}
    if payload_error(record):
        return {**out, "error": payload_error(record)}
    items = candidates(record)
    if items:
        out["candidates"] = copy.deepcopy(items[:25])
        for item in out["candidates"]:
            if isinstance(item.get("author"), list):
                item["author"] = [
                    {k: v for k, v in author.items() if k in {"given", "family", "literal", "name"}}
                    if isinstance(author, dict)
                    else author
                    for author in item["author"]
                ]
        if len(items) > 25:
            out["candidate_projection_note"] = (
                f"Showing 25 of {len(items)} retained candidates; full source remains available"
            )
    if record.get("metadata"):
        out["metadata"] = {k: v for k, v in record["metadata"].items() if k != "citation_abstract"}
    notes = bibliographic_notes(record)
    if notes:
        out["bibliographic_notes"] = notes
    if record.get("text") and not out.get("metadata") and not items:
        out["text"] = record["text"][:3500]
    extent = search_extent(record)
    if extent:
        out["search_extent"] = extent
        if extent.get("official_scope"):
            out["local_title_query"] = "Use Read/Grep on the complete retained volume to inspect matching titles."
    if record.get("error") or payload_error(record):
        out["error"] = record.get("error") or payload_error(record)
    return preview_packet({"sources": [out]})["sources"][0]


def model_input(refs, root, search) -> Any:
    from .syntax import syntax_issues

    sources = catalog(root)
    # The catalog deduplicates semantic records by source ID. Initial and cached
    # evidence can contain the same record under different paths/JSON formatting;
    # every original path must still resolve to that shared source ID.
    by_path = {
        path.relative_to(root).as_posix(): source_id(json.loads(path.read_text()))
        for path in (root / "evidence").rglob("*.json")
    }
    return {
        "search_allowed": search,
        "references": [
            {
                "id": r["id"],
                "raw": r["raw"],
                "required_field_checks": required_fields(r["raw"]),
                "supplied_identifiers": supplied_identifiers(r["raw"]),
                "malformed_identifiers": syntax_issues(r["raw"]),
                "source_ids": list(dict.fromkeys(by_path[e["evidence_file"]] for e in r.get("prefetched", []))),
            }
            for r in refs
        ],
        "sources": [{**compact(v["record"]), "evidence_file": v["evidence_file"]} for v in sources.values()],
    }


def supplied_identifiers(raw) -> Any:
    flat = re.sub(r"/\s+", "/", raw)
    dois = [v.rstrip(".,;").casefold() for v in re.findall(r"10\.\d{4,9}/[^\s]+", flat)]
    arxiv = re.findall(r"(?:arxiv\s*:\s*|arxiv\.org/(?:abs|pdf)/)(\d{4}\.\s*\d{4,5}(?:v\d+)?)", flat, re.IGNORECASE)
    return list(
        dict.fromkeys(["doi:" + v for v in dois] + ["arxiv:" + re.sub(r"\s+", "", v).casefold() for v in arxiv])
    )


def normalized_title(value) -> Any:
    if isinstance(value, list):
        value = value[0] if value else ""
    return "".join(c for c in unicodedata.normalize("NFKD", str(value)).casefold() if c.isalnum())


def required_fields(raw) -> Any:
    fields = {"title"}
    if re.search(r"10\.\d{4,9}/|arxiv\s*:|arxiv\.org/(abs|pdf)/", raw, re.IGNORECASE):
        fields.add("identifier")
    without_urls = re.sub(r"https?://\S+|10\.\d{4,9}/\S+", "", raw)
    if re.search(r"\b(?:19|20)\d{2}\b", without_urls):
        fields.add("year")
    for pattern, field in (
        (r"(?im)^authors?\s*:", "authors"),
        (r"(?im)^(venue|journal|booktitle)\s*:", "venue"),
        (r"(?im)^pages\s*:|\bpp?\.\s*\d", "pages"),
    ):
        if re.search(pattern, raw):
            fields.add(field)
    return sorted(fields)


def preview_packet(packet) -> Any:
    packet = copy.deepcopy(packet)
    for source in packet["sources"]:
        projected = []
        for candidate in source.get("candidates", []):
            if not isinstance(candidate, dict):
                continue
            # DOI CSL responses can embed an entire cited bibliography. Those
            # records are not additional candidates for the supplied reference.
            fields = {
                "candidate_id",
                "title",
                "subtitle",
                "author",
                "authors",
                "editor",
                "year",
                "published",
                "published-print",
                "published-online",
                "issued",
                "container-title",
                "venue",
                "container",
                "publisher",
                "DOI",
                "doi",
                "URL",
                "url",
                "type",
                "page",
                "pages",
                "volume",
                "issue",
                "number",
                "ISBN",
                "ISSN",
                "edition",
                "version",
                "dates",
                "event",
            }
            candidate = {k: v for k, v in candidate.items() if k in fields}
            for field in ("author", "authors"):
                authors = candidate.get(field)
                if isinstance(authors, list) and len(authors) > 12:
                    candidate[field] = authors[:12]
                    candidate["author_preview_note"] = (
                        f"Showing 12 of {len(authors)} authors. Inspect the full source before confirming a complete author list."
                    )
            projected.append(candidate)
        if "candidates" in source:
            source["candidates"] = projected
            source["candidate_projection_note"] = (
                "Bibliographic fields only; full response remains at the listed source path."
            )
    return packet
